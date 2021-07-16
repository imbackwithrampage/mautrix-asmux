# mautrix-asmux - A Matrix application service proxy and multiplexer
# Copyright (C) 2021 Beeper, Inc. All rights reserved.
import json
from typing import Any, Dict, Union, Optional
from uuid import UUID
import logging
import asyncio

import aiohttp
from yarl import URL
from aiohttp import web
from aiohttp.http import WSCloseCode

from mautrix.util.bridge_state import BridgeState
from mautrix.util.logging import TraceLogger
from mautrix.errors import make_request_error

from ..database import AppService
from ..config import Config
from .cs_proxy import ClientProxy
from .errors import Error
from .as_proxy import Events
from .websocket_util import WebsocketHandler

WS_CLOSE_REPLACED = 4001


class AppServiceWebsocketHandler:
    log: TraceLogger = logging.getLogger("mau.api.as_websocket")
    websockets: dict[UUID, WebsocketHandler]
    status_endpoint: Optional[str]
    sync_proxy: Optional[URL]
    sync_proxy_token: Optional[str]
    sync_proxy_own_address: Optional[str]
    hs_token: str
    hs_domain: str
    mxid_prefix: str
    mxid_suffix: str
    _stopping: bool

    def __init__(self, config: Config, mxid_prefix: str, mxid_suffix: str) -> None:
        self.status_endpoint = config["mux.status_endpoint"]
        self.sync_proxy = (URL(config["mux.sync_proxy.url"]) if config["mux.sync_proxy.url"]
                           else None)
        self.sync_proxy_token = config["mux.sync_proxy.token"]
        self.sync_proxy_own_address = config["mux.sync_proxy.asmux_address"]
        self.hs_token = config["appservice.hs_token"]
        self.mxid_prefix = mxid_prefix
        self.mxid_suffix = mxid_suffix
        self.websockets = {}
        self.requests = {}
        self._stopping = False

    async def stop(self) -> None:
        self._stopping = True
        self.log.debug("Disconnecting websockets")
        await asyncio.gather(*(ws.close(code=WSCloseCode.SERVICE_RESTART,
                                        status="server_shutting_down")
                               for ws in self.websockets.values()))

    async def send_bridge_status(self, az: AppService, state: Union[dict[str, Any], BridgeState]
                                 ) -> None:
        if not self.status_endpoint:
            return
        if not isinstance(state, BridgeState):
            state = BridgeState.deserialize(state)
        self.log.debug(f"Sending bridge status for {az.name} to API server: {state}")
        await state.send(url=self.status_endpoint.format(owner=az.owner, prefix=az.prefix),
                         token=az.real_as_token, log=self.log, log_sent=False)

    @staticmethod
    async def _get_response(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
        text = await resp.text()
        errcode = error = resp_data = None
        try:
            resp_data = await resp.json()
            errcode = resp_data["errcode"]
            error = resp_data["error"]
        except (json.JSONDecodeError, aiohttp.ContentTypeError, KeyError, TypeError):
            pass
        if resp.status >= 400:
            raise make_request_error(resp.status, text, errcode, error)
        return resp_data

    async def start_sync_proxy(self, az: AppService, data: Dict[str, Any]) -> Dict[str, Any]:
        url = self.sync_proxy.with_path("/_matrix/client/unstable/fi.mau.syncproxy") / str(az.id)
        headers = {"Authorization": f"Bearer {self.sync_proxy_token}"}
        req = {
            "appservice_id": str(az.id),
            "user_id": f"{self.mxid_prefix}{az.owner}_{az.prefix}_{az.bot}{self.mxid_suffix}",
            "bot_access_token": data["access_token"],
            "device_id": data["device_id"],
            "hs_token": self.hs_token,
            "address": self.sync_proxy_own_address,
            "is_proxy": True,
        }
        self.log.debug(f"Requesting sync proxy start for {az.id}")
        self.log.trace("Sync proxy data: %s", req)
        async with aiohttp.ClientSession() as sess, sess.put(url, json=req, headers=headers
                                                             ) as resp:
            return await self._get_response(resp)

    async def stop_sync_proxy(self, az: AppService) -> None:
        url = self.sync_proxy.with_path("/_matrix/client/unstable/fi.mau.syncproxy") / str(az.id)
        headers = {"Authorization": f"Bearer {self.sync_proxy_token}"}
        self.log.debug(f"Requesting sync proxy stop for {az.id}")
        try:
            async with aiohttp.ClientSession() as sess, sess.delete(url, headers=headers) as resp:
                await self._get_response(resp)
            self.log.debug(f"Stopped sync proxy for {az.id}")
        except Exception as e:
            self.log.warning(f"Failed to request sync proxy stop for {az.id}: "
                             f"{type(e).__name__}: {e}")
            self.log.trace("Sync proxy stop error", exc_info=True)

    async def handle_ws(self, req: web.Request) -> web.WebSocketResponse:
        if self._stopping:
            raise Error.server_shutting_down
        az = await ClientProxy.find_appservice(req, raise_errors=True)
        if az.push:
            raise Error.appservice_ws_not_enabled
        ws = WebsocketHandler(type_name="Websocket transaction connection",
                              proto="fi.mau.as_sync",
                              log=self.log.getChild(az.name))
        ws.set_handler("bridge_status", lambda handler, data: self.send_bridge_status(az, data))
        ws.set_handler("start_sync", lambda handler, data: self.start_sync_proxy(az, data))
        await ws.prepare(req)
        log = self.log.getChild(az.name)
        if az.id in self.websockets:
            log.debug(f"New websocket connection coming in, closing old one")
            await self.websockets[az.id].close(code=WS_CLOSE_REPLACED, status="conn_replaced")
        try:
            self.websockets[az.id] = ws
            await ws.send(command="connect", status="connected")
            await ws.handle()
        finally:
            if self.websockets.get(az.id) == ws:
                del self.websockets[az.id]
                asyncio.create_task(self.stop_sync_proxy(az))
                if not self._stopping:
                    # TODO figure out remote IDs properly
                    await self.send_bridge_status(az, BridgeState(
                        ok=False, error="websocket-not-connected",  # remote_id="*"
                    ).fill())
        return ws.response

    async def post_events(self, appservice: AppService, events: Events) -> str:
        try:
            ws = self.websockets[appservice.id]
        except KeyError:
            # TODO buffer transactions
            self.log.warning(f"Not sending transaction {events.txn_id} to {appservice.name}: "
                             f"websocket not connected")
            return "websocket-not-connected"
        self.log.debug(f"Sending transaction {events.txn_id} to {appservice.name} via websocket")
        try:
            await ws.send(raise_errors=True, command="transaction", status="ok",
                          txn_id=events.txn_id, **events.serialize())
        except Exception:
            return "websocket-send-fail"
        return "ok"

    async def ping(self, appservice: AppService, remote_id: str) -> BridgeState:
        try:
            ws = self.websockets[appservice.id]
        except KeyError:
            return BridgeState(ok=False, error="websocket-not-connected").fill()
        try:
            raw_pong = await asyncio.wait_for(ws.request("ping", remote_id=remote_id), timeout=45)
        except asyncio.TimeoutError:
            return BridgeState(ok=False, error="io-timeout").fill()
        except Exception as e:
            return BridgeState(ok=False, error="websocket-fatal-error", message=str(e)).fill()
        return BridgeState.deserialize(raw_pong)
