# mautrix-asmux - A Matrix application service proxy and multiplexer
# Copyright (C) 2021 Beeper, Inc. All rights reserved.
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union, cast
from uuid import UUID
import asyncio
import json
import logging
import time

from aiohttp import web
from aiohttp.http import WSCloseCode
from aioredis import Redis
from yarl import URL
import aiohttp

from mautrix.api import HTTPAPI
from mautrix.errors import (
    MatrixStandardRequestError,
    MNotFound,
    make_request_error,
    standard_error,
)
from mautrix.types import JSON
from mautrix.util.bridge_state import BridgeState, BridgeStateEvent, GlobalBridgeState
from mautrix.util.logging import TraceLogger
from mautrix.util.message_send_checkpoint import (
    MessageSendCheckpoint,
    MessageSendCheckpointReportedBy,
    MessageSendCheckpointStatus,
    MessageSendCheckpointStep,
)
from mautrix.util.opt_prometheus import Counter, Gauge

from ..config import Config
from ..database import AppService
from ..sygnal import PushKey
from ..util import log_task_exceptions
from .as_proxy import migrate_state_data, send_message_checkpoints
from .as_queue import AppServiceQueue
from .as_util import Events, make_ping_error, send_failed_metrics, send_successful_metrics
from .cs_proxy import ClientProxy
from .errors import Error, WebsocketNotConnected
from .websocket_util import WebsocketHandler

if TYPE_CHECKING:
    from ..server import MuxServer

# Response timeout when sending an event via websocket for the first time.
FIRST_SEND_TIMEOUT = 5
# Response timeout when retrying sends.
RETRY_SEND_TIMEOUT = 30
# Allow client to not respond for ~3 minutes before websocket is disconnected
TIMEOUT_COUNT_LIMIT = 7
# Minimum number of seconds between all wakeup pushes
MIN_WAKEUP_PUSH_DELAY = 3

WS_CLOSE_REPLACED = 4001
WS_NOT_ACKNOWLEDGED = 4002
CONNECTED_WEBSOCKETS = Gauge(
    "asmux_connected_websockets",
    "Bridges connected to the appservice transaction websocket",
    labelnames=["owner", "bridge"],
)


@standard_error("FI.MAU.SYNCPROXY.NOT_ACTIVE")
class SyncProxyNotActive(MatrixStandardRequestError):
    pass


class AppServiceWebsocketHandler:
    log: TraceLogger = cast(TraceLogger, logging.getLogger("mau.api.as_websocket"))
    websockets: dict[UUID, WebsocketHandler]
    queues: dict[UUID, AppServiceQueue]
    prev_wakeup_push: dict[UUID, float]
    remote_status_endpoint: Optional[str]
    bridge_status_endpoint: Optional[str]
    sync_proxy: URL
    sync_proxy_token: Optional[str]
    sync_proxy_own_address: Optional[str]
    hs_token: str
    hs_domain: str
    mxid_prefix: str
    mxid_suffix: str
    _stopping: bool
    checkpoint_url: str
    api_server_sess: aiohttp.ClientSession
    sync_proxy_sess: aiohttp.ClientSession

    def __init__(
        self,
        server: "MuxServer",
        config: Config,
        mxid_prefix: str,
        mxid_suffix: str,
        redis: Redis,
    ) -> None:
        self.server = server
        self.remote_status_endpoint = config["mux.remote_status_endpoint"]
        self.bridge_status_endpoint = config["mux.bridge_status_endpoint"]
        self.sync_proxy = URL(config["mux.sync_proxy.url"])
        self.sync_proxy_token = config["mux.sync_proxy.token"]
        self.sync_proxy_own_address = config["mux.sync_proxy.asmux_address"]
        self.hs_token = config["appservice.hs_token"]
        self.redis = redis
        self.mxid_prefix = mxid_prefix
        self.mxid_suffix = mxid_suffix
        self.websockets = {}
        self.queues = {}
        self.prev_wakeup_push = {}
        self._stopping = False
        self.checkpoint_url = config["mux.message_send_checkpoint_endpoint"]
        self.api_server_sess = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20), headers={"User-Agent": HTTPAPI.default_ua}
        )
        self.sync_proxy_sess = aiohttp.ClientSession(headers={"User-Agent": HTTPAPI.default_ua})

    async def stop(self) -> None:
        self._stopping = True
        self.log.debug("Disconnecting websockets")
        await asyncio.gather(
            *(
                ws.close(code=WSCloseCode.SERVICE_RESTART, status="server_shutting_down")
                for ws in self.websockets.values()
            )
        )

    async def send_remote_status(
        self, az: AppService, state: Union[dict[str, Any], BridgeState]
    ) -> None:
        if not self.remote_status_endpoint:
            return
        if not isinstance(state, BridgeState):
            state = BridgeState.deserialize(migrate_state_data(state, is_global=False))
        self.log.debug(f"Sending remote status for {az.name} to API server: {state}")
        await state.send(
            url=self.remote_status_endpoint.format(owner=az.owner, prefix=az.prefix),
            token=az.real_as_token,
            log=self.log,
            log_sent=False,
        )

    def send_bridge_unreachable_status(self, az: AppService) -> None:
        # If we've lost the websocket, we won't be able to send events from matrix to the
        # bridge anymore. Let the api-server know so it can let the user know.

        # androidsms should not do this, as the websocket is not necessary for
        # connectivity, as we send a push notification to the app every time it should
        # do something with the websocket. This means there's no loss of functionality
        # when the websocket goes down, so we should not notify the user.
        if az.prefix == "androidsms":
            return

        async def check_and_send_bridge_unreachable_status() -> None:
            # NOTE: this function will retry pinging with a 30s timeout, so we don't need to
            # sleep here anymore.
            # Only continue on to report unreachable if the websocket is still disconnected. If
            # it's been re-established in the time it took us to handle this async action, do
            # nothing.
            ping = await self.server.as_requester.ping(az)
            if ping.bridge_state.state_event == BridgeStateEvent.BRIDGE_UNREACHABLE:
                await self.send_bridge_status(az, BridgeStateEvent.BRIDGE_UNREACHABLE)

        asyncio.create_task(check_and_send_bridge_unreachable_status())

    async def send_bridge_status(self, az: AppService, state_event: BridgeStateEvent) -> None:
        if not self.bridge_status_endpoint:
            return
        headers = {"Authorization": f"Bearer {az.real_as_token}"}
        body = {"stateEvent": state_event.serialize()}
        url = self.bridge_status_endpoint.format(owner=az.owner, prefix=az.prefix)
        self.log.debug(f"Sending bridge status for {az.name} to API server {url}: {state_event}")
        try:
            async with self.api_server_sess.post(url, json=body, headers=headers) as resp:
                if not 200 <= resp.status < 300:
                    text = await resp.text()
                    text = text.replace("\n", "\\n")
                    self.log.warning(
                        f"Unexpected status code {resp.status} sending bridge state update: {text}"
                    )
        except Exception as e:
            self.log.warning(f"Failed to send updated bridge state: {e}")

    @staticmethod
    async def _get_response(resp: aiohttp.ClientResponse) -> Optional[Dict[str, Any]]:
        text = await resp.text()
        errcode = ""
        error = ""
        resp_data = None
        try:
            resp_data = await resp.json()
            errcode = resp_data["errcode"]
            error = resp_data["error"]
        except (json.JSONDecodeError, aiohttp.ContentTypeError, KeyError, TypeError):
            pass
        if resp.status >= 400:
            raise make_request_error(resp.status, text, errcode, error)
        return resp_data

    async def start_sync_proxy(
        self, az: AppService, data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
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
        self.log.debug(f"Requesting sync proxy start for {az.name}")
        self.log.trace("Sync proxy data: %s", req)
        async with self.sync_proxy_sess.put(url, json=req, headers=headers) as resp:
            return await self._get_response(resp)

    async def ping_server(self, az: AppService, ws: WebsocketHandler) -> Dict[str, Any]:
        current_ws = self.websockets.get(az.id)
        current_id = current_ws.identifier if current_ws else None
        assert ws == current_ws, f"websocket {ws.identifier} is not current ({current_id})"
        return {"timestamp": int(time.time() * 1000)}

    async def stop_sync_proxy(self, az: AppService) -> None:
        url = self.sync_proxy.with_path("/_matrix/client/unstable/fi.mau.syncproxy") / str(az.id)
        headers = {"Authorization": f"Bearer {self.sync_proxy_token}"}
        self.log.debug(f"Requesting sync proxy stop for {az.name}")
        try:
            async with self.sync_proxy_sess.delete(url, headers=headers) as resp:
                await self._get_response(resp)
            self.log.debug(f"Stopped sync proxy for {az.name}")
        except (MNotFound, SyncProxyNotActive) as e:
            self.log.debug(f"Failed to request sync proxy stop for {az.name}: {e}")
        except Exception as e:
            self.log.warning(
                f"Failed to request sync proxy stop for {az.name}: {type(e).__name__}: {e}"
            )
            self.log.trace("Sync proxy stop error", exc_info=True)

    async def set_push_key(self, az: AppService, data: JSON) -> None:
        self.log.info(f"Setting puskey for {az.name}")
        await az.set_push_key(PushKey.deserialize(data))

    def has_az_websocket(self, az: AppService) -> bool:
        return az.id in self.websockets

    async def close_stale_az_websocket(self, az: AppService) -> None:
        """
        Close any active websocket for this appservice as a new one has come in.
        """
        try:
            # Popping the websocket is essential here which will prevent the handler
            # from stopping sync proxy (this is OK because we call this function
            # when a new websocket connection is opened for this appservice).
            ws = self.websockets.pop(az.id)
        except KeyError:
            return
        ws.log.debug("New websocket connection coming in, closing old one")
        await ws.close(code=WS_CLOSE_REPLACED, status="conn_replaced")

    async def handle_ws(self, req: web.Request) -> web.WebSocketResponse:
        if self._stopping:
            raise Error.server_shutting_down
        az = await ClientProxy.find_appservice(req, raise_errors=True)
        assert az is not None
        if az.push:
            raise Error.appservice_ws_not_enabled
        identifier = req.headers.get("X-Mautrix-Process-ID", "unidentified")
        proto_version = int(req.headers.get("X-Mautrix-Websocket-Version", "1"))
        ws = WebsocketHandler(
            type_name="Websocket transaction connection",
            proto="fi.mau.as_sync",
            version=proto_version,
            log=self.log.getChild(az.name).getChild(identifier),
            identifier=identifier,
            heartbeat=60 if az.prefix == "imessagecloud" else None,
        )
        ws.set_handler("bridge_status", lambda _, data: self.send_remote_status(az, data))  # type: ignore
        ws.set_handler(
            "message_checkpoint", lambda _, data: send_message_checkpoints(self, az, data)  # type: ignore
        )
        ws.set_handler("push_key", lambda _, data: self.set_push_key(az, data))  # type: ignore
        ws.set_handler("start_sync", lambda _, data: self.start_sync_proxy(az, data))  # type: ignore
        ws.set_handler("ping", lambda ws, _: self.ping_server(az, ws))  # type: ignore
        await ws.prepare(req)
        # Close out any other open websockets for this appservice, first on the
        # local instance then via Redis to other asmux instances.
        await self.close_stale_az_websocket(az)
        await self.server.as_requester.close_other_stale_az_websockets(az)
        try:
            self.websockets[az.id] = ws
            CONNECTED_WEBSOCKETS.labels(owner=az.owner, bridge=az.prefix).inc()
            await ws.send(command="connect", status="connected")
            asyncio.create_task(self._consume_queue(az, ws))
            await ws.handle()
        except Exception as e:
            ws.log.warning(f"Exception in websocket handler: {e}")
        finally:
            ws.log.debug("Websocket handler finished")
            ws.dead = True
            CONNECTED_WEBSOCKETS.labels(owner=az.owner, bridge=az.prefix).dec()
            if self.websockets.get(az.id) == ws:
                del self.websockets[az.id]

                asyncio.create_task(self.stop_sync_proxy(az))
                if not self._stopping:
                    self.send_bridge_unreachable_status(az)

        return ws.response

    def _send_metrics(self, az: AppService, txn: Events, metric: Counter) -> None:
        for type in txn.types:
            metric.labels(owner=az.owner, bridge=az.prefix, type=type).inc()

    async def _send_next_txn(
        self, az: AppService, ws: WebsocketHandler, txn: Events, timeout: int
    ) -> None:
        ws.log.debug(f"Sending transaction {txn.txn_id} to {az.name} via websocket")
        data = {"status": "ok", "txn_id": txn.txn_id, **txn.serialize()}
        if ws.proto >= 3:
            await asyncio.wait_for(
                ws.request("transaction", top_level_data=data, raise_errors=True),
                timeout=timeout,
            )
        elif ws.proto >= 2:
            # Legacy protocol where client can't handle duplicate transactions properly,
            # so we can't safely retry on timeout.
            try:
                await asyncio.wait_for(
                    ws.request("transaction", top_level_data=data, raise_errors=True),
                    timeout=RETRY_SEND_TIMEOUT,
                )
            except asyncio.TimeoutError:
                ws.log.warning(
                    f"Failed to send {txn.txn_id} to {az.name}: "
                    f"didn't get response within {RETRY_SEND_TIMEOUT} seconds"
                    f" -- legacy protocol, dropping transaction"
                )
                ws.timeouts += 1
                send_failed_metrics(az, txn)
                return
        else:
            # Legacy protocol where client doesn't send acknowledgements
            await ws.send(raise_errors=True, command="transaction", **data)
        ws.timeouts = 0
        self.log.debug(f"Successfully sent {txn.txn_id} to {az.name}")
        send_successful_metrics(az, txn)

    def get_queue(self, az: AppService) -> AppServiceQueue:
        return self.queues.setdefault(
            az.id,
            AppServiceQueue(redis=self.redis, mxid_suffix=self.mxid_suffix, az=az),
        )

    async def report_expired_pdu(self, az: AppService, expired: List[JSON]) -> None:
        checkpoints = [
            MessageSendCheckpoint(
                event_id=evt.get("event_id"),
                room_id=evt.get("room_id"),
                step=MessageSendCheckpointStep.BRIDGE,
                timestamp=int(time.time() * 1000),
                status=MessageSendCheckpointStatus.TIMEOUT,
                event_type=evt.get("type"),
                reported_by=MessageSendCheckpointReportedBy.ASMUX,
                info="dropped old event",
            ).serialize()
            for evt in expired
        ]
        await send_message_checkpoints(self, az, {"checkpoints": checkpoints})

    async def _consume_queue_one(
        self, az: AppService, ws: WebsocketHandler, queue: AppServiceQueue
    ) -> None:
        timeout = FIRST_SEND_TIMEOUT if ws.timeouts == 0 else RETRY_SEND_TIMEOUT
        txn: Optional[Events] = None
        try:
            async with queue.next() as txn:
                if not txn:
                    return
                expired = txn.pop_expired_pdu(queue.owner_mxid)
                if expired:
                    self.log.warning(f"Dropped {len(expired)} expired PDUs")
                    asyncio.create_task(
                        log_task_exceptions(self.log, self.report_expired_pdu(az, expired)),
                    )
                if not txn.is_empty:
                    await self._send_next_txn(az, ws, txn, timeout)
        except asyncio.TimeoutError:
            ws.log.warning(
                f"Failed to send {txn.txn_id} to {az.name}: "  # type: ignore
                f"didn't get response within {timeout} seconds",
            )
            ws.timeouts += 1
            if ws.timeouts >= TIMEOUT_COUNT_LIMIT:
                asyncio.create_task(
                    ws.close(code=WS_NOT_ACKNOWLEDGED, status="transactions_not_acknowledged")
                )
                return
            elif self.should_wakeup(az):
                await self.server.as_requester.wakeup_appservice(az)
        except Exception:
            if txn is None:
                ws.log.exception("Failed to get next transaction")
                raise
            else:
                ws.log.exception(f"Failed to send {txn.txn_id} to {az.name}")

    async def _consume_queue(self, az: AppService, ws: WebsocketHandler) -> None:
        queue = self.get_queue(az)
        ws.log.debug("Started consuming events from queue")

        try:
            while not ws.dead:
                await self._consume_queue_one(az, ws, queue)
        except Exception:
            self.log.exception("Fatal error in queue consumer")
        finally:
            if not ws.dead:
                ws.log.critical("Queue consumer stopped but websocket not dead, closing!")
                asyncio.create_task(
                    ws.close(code=WSCloseCode.INTERNAL_ERROR, status="queue_consumer_failed")
                )

    def should_wakeup(
        self,
        az: AppService,
        only_if_ws_timeout: bool = False,
        min_time_since_last_push: int = MIN_WAKEUP_PUSH_DELAY,
        min_time_since_ws_message: int = RETRY_SEND_TIMEOUT,
    ) -> bool:
        if not az.push_key:
            return False
        now = time.time()
        try:
            ws = self.websockets[az.id]
        except KeyError:
            pass
        else:
            if only_if_ws_timeout and ws.timeouts == 0:
                return False
            elif ws.last_received + min_time_since_ws_message > now:
                return False
        if self.prev_wakeup_push.get(az.id, 0) + min_time_since_last_push > now:
            return False
        return True

    def set_prev_wakeup_push(self, az: AppService) -> None:
        self.prev_wakeup_push[az.id] = time.time()

    async def post_syncproxy_error(self, az: AppService, txn_id: str, data: dict[str, Any]) -> str:
        try:
            ws = self.websockets[az.id]
        except KeyError:
            self.log.warning(
                f"Not sending syncproxy error {txn_id} to {az.name}: websocket not connected"
            )
            return "websocket-not-connected"
        self.log.debug(f"Sending transaction {txn_id} to {az.name} via websocket")
        try:
            if ws.proto >= 2:
                await asyncio.wait_for(
                    ws.request("syncproxy_error", txn_id=txn_id, **data),
                    timeout=RETRY_SEND_TIMEOUT,
                )
            else:
                # Legacy API where client doesn't send acknowledgements
                await ws.send(raise_errors=True, command="transaction", **data)
        except asyncio.TimeoutError:
            ws.timeouts += 1
            return "websocket-send-fail"
        except Exception:
            return "websocket-send-fail"
        return "ok"

    async def post_command(
        self, az: AppService, command: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            ws = self.websockets[az.id]
        except KeyError:
            raise WebsocketNotConnected()
        return await asyncio.wait_for(
            ws.request(command, raise_errors=True, **data),
            timeout=10,
        )

    async def ping(self, az: AppService) -> GlobalBridgeState:
        try:
            ws = self.websockets[az.id]
        except KeyError:
            return make_ping_error("websocket-not-connected")
        try:
            raw_pong = await asyncio.wait_for(ws.request("ping"), timeout=45)
        except asyncio.TimeoutError:
            return make_ping_error("io-timeout")
        except Exception as e:
            self.log.warning(f"Failed to ping {az.name} ({az.id}) via websocket", exc_info=True)
            return make_ping_error("websocket-fatal-error", message=str(e))
        if raw_pong:
            return GlobalBridgeState.deserialize(migrate_state_data(raw_pong))
        self.log.warning(f"Failed to ping {az.name} ({az.id}) via websocket")
        return make_ping_error("websocket-unknown-error")
