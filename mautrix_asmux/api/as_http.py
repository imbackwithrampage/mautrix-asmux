# mautrix-asmux - A Matrix application service proxy and multiplexer
# Copyright (C) 2021 Beeper, Inc. All rights reserved.
from typing import Any
import logging
import asyncio
import json

from yarl import URL
from aiohttp import ClientError, ClientTimeout, ContentTypeError
import aiohttp

from mautrix.util.bridge_state import GlobalBridgeState

from ..database import AppService
from .as_proxy import Events, make_ping_error, migrate_state_data
from .errors import Error


class AppServiceHTTPHandler:
    log: logging.Logger = logging.getLogger("mau.api.as_http")
    http: aiohttp.ClientSession
    mxid_suffix: str

    def __init__(self, mxid_suffix: str, http: aiohttp.ClientSession) -> None:
        self.mxid_suffix = mxid_suffix
        self.http = http

    async def post_events(self, appservice: AppService, events: Events) -> str:
        attempt = 0
        url = URL(appservice.address) / "_matrix/app/v1/transactions" / events.txn_id
        err_prefix = (f"Failed to send transaction {events.txn_id} "
                      f"({len(events.pdu)}p/{len(events.edu)}e) to {url}")
        retries = 10 if len(events.pdu) > 0 else 2
        backoff = 1
        last_error = ""
        while attempt < retries:
            attempt += 1
            self.log.debug(f"Sending transaction {events.txn_id} to {appservice.name} "
                           f"via HTTP, attempt #{attempt}")
            try:
                resp = await self.http.put(url.with_query({"access_token": appservice.hs_token}),
                                           json=events.serialize())
            except ClientError as e:
                last_error = e
                self.log.debug(f"{err_prefix}: {last_error}")
            except Exception:
                last_error = None
                self.log.exception(f"{err_prefix}")
                break
            else:
                if resp.status >= 400:
                    last_error = f"HTTP {resp.status}: {await resp.text()!r}"
                    self.log.debug(f"{err_prefix}: {last_error}")
                else:
                    return "ok"
            # Don't sleep after last attempt
            if attempt < retries:
                await asyncio.sleep(backoff)
                backoff *= 1.5
        last_error = f" (last error: {last_error})" if last_error else ""
        self.log.warning(f"Gave up trying to send {events.txn_id} to {appservice.name}"
                         + last_error)
        return "http-gave-up"

    async def post_syncproxy_error(self, appservice: AppService, txn_id: str, data: dict[str, Any]
                                   ) -> str:
        raise Error.syncproxy_error_not_supported

    async def ping(self, appservice: AppService) -> GlobalBridgeState:
        url = (URL(appservice.address) / "_matrix/app/com.beeper.bridge_state").with_query({
            "user_id": f"@{appservice.owner}{self.mxid_suffix}",
            # TODO remove after making sure it's safe to remove
            "remote_id": "",
        })
        headers = {"Authorization": f"Bearer {appservice.hs_token}"}
        try:
            resp = await self.http.post(url, headers=headers, timeout=ClientTimeout(total=45))
        except asyncio.TimeoutError:
            return make_ping_error("io-timeout")
        except ClientError as e:
            return make_ping_error("http-connection-error", message=str(e))
        except Exception as e:
            self.log.warning(f"Failed to ping {appservice.name} ({appservice.id}) via HTTP",
                             exc_info=True)
            return make_ping_error("http-fatal-error", message=str(e))
        try:
            raw_pong = await resp.json()
        except (json.JSONDecodeError, ContentTypeError):
            if resp.status >= 300:
                return make_ping_error(f"ping-http-{resp.status}",
                                       f"Ping returned non-JSON body and HTTP {resp.status}")
            return make_ping_error("http-not-json")
        return GlobalBridgeState.deserialize(migrate_state_data(raw_pong))
