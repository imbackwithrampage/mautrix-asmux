# mautrix-asmux - A Matrix application service proxy and multiplexer
# Copyright (C) 2021 Beeper, Inc. All rights reserved.
from typing import Optional, List, Dict, Any, Awaitable, TYPE_CHECKING
from collections import defaultdict
from uuid import UUID
import logging
import asyncio

import aiohttp
import attr
from attr import dataclass

from mautrix.types import JSON, DeviceOTKCount, DeviceLists, UserID
from mautrix.appservice import AppServiceServerMixin
from mautrix.util.opt_prometheus import Counter

from ..database import Room, AppService
from ..segment import track_events

if TYPE_CHECKING:
    from ..server import MuxServer


@dataclass
class Events:
    txn_id: str
    pdu: List[JSON] = attr.ib(factory=lambda: [])
    edu: List[JSON] = attr.ib(factory=lambda: [])
    types: List[str] = attr.ib(factory=lambda: [])
    otk_count: Dict[UserID, DeviceOTKCount] = attr.ib(factory=lambda: {})
    device_lists: DeviceLists = attr.ib(factory=lambda: DeviceLists(changed=[], left=[]))

    def serialize(self) -> Dict[str, Any]:
        output = {
            "events": self.pdu
        }
        if self.edu:
            output["ephemeral"] = self.edu
        if self.otk_count:
            output["one_time_keys_count"] = self.otk_count
        if self.device_lists.changed or self.device_lists.left:
            output["device_lists"] = self.device_lists
        return output


RECEIVED_EVENTS = Counter("asmux_received_events", "Number of incoming events",
                          labelnames=["type"])
DROPPED_EVENTS = Counter("asmux_dropped_events", "Number of events with no target appservice",
                         labelnames=["type"])
ACCEPTED_EVENTS = Counter("asmux_accepted_events",
                          "Number of events that have a target appservice",
                          labelnames=["owner", "bridge", "type"])
SUCCESSFUL_EVENTS = Counter("asmux_successful_events",
                            "Number of PDUs that were successfully sent to the target appservice",
                            labelnames=["owner", "bridge", "type"])
FAILED_EVENTS = Counter("asmux_failed_events",
                        "Number of PDUs that were successfully sent to the target appservice",
                        labelnames=["owner", "bridge", "type"])


class AppServiceProxy(AppServiceServerMixin):
    log: logging.Logger = logging.getLogger("mau.api.as_proxy")
    http: aiohttp.ClientSession

    hs_token: str
    mxid_prefix: str
    mxid_suffix: str
    locks: Dict[UUID, asyncio.Lock]

    def __init__(self, server: 'MuxServer', mxid_prefix: str, mxid_suffix: str, hs_token: str,
                 http: aiohttp.ClientSession) -> None:
        super().__init__(ephemeral_events=True)
        self.server = server
        self.mxid_prefix = mxid_prefix
        self.mxid_suffix = mxid_suffix
        self.hs_token = hs_token
        self.http = http
        self.locks = defaultdict(lambda: asyncio.Lock())

    async def post_events(self, appservice: AppService, events: Events) -> bool:
        async with self.locks[appservice.id]:
            for type in events.types:
                ACCEPTED_EVENTS.labels(owner=appservice.owner, bridge=appservice.prefix,
                                       type=type).inc()
            ok = False
            try:
                if not appservice.push:
                    ok = await self.server.as_websocket.post_events(appservice, events)
                elif appservice.address:
                    ok = await self.server.as_http.post_events(appservice, events)
                else:
                    self.log.warning(f"Not sending transaction {events.txn_id} "
                                     f"to {appservice.name}: no address configured")
            except Exception:
                self.log.exception(f"Fatal error sending transaction {events.txn_id} "
                                   f"to {appservice.name}")
            if ok:
                self.log.debug(f"Successfully sent {events.txn_id} to {appservice.name}")
                asyncio.create_task(track_events(appservice, events))
            metric = SUCCESSFUL_EVENTS if ok else FAILED_EVENTS
            for type in events.types:
                metric.labels(owner=appservice.owner, bridge=appservice.prefix, type=type).inc()
            return ok

    async def _get_az_from_user_id(self, user_id: UserID) -> Optional[AppService]:
        if ((not user_id or not user_id.startswith(self.mxid_prefix)
             or not user_id.endswith(self.mxid_suffix))):
            return None
        localpart: str = user_id[len(self.mxid_prefix):-len(self.mxid_suffix)]
        try:
            owner, prefix, _ = localpart.split("_", 2)
        except ValueError:
            return None
        return await AppService.find(owner, prefix)

    async def register_room(self, event: JSON) -> Optional[Room]:
        try:
            if ((event["type"] != "m.room.member"
                 or not event["state_key"].startswith(self.mxid_prefix))):
                return None
        except KeyError:
            return None
        user_id: UserID = event["state_key"]
        az = await self._get_az_from_user_id(user_id)
        if not az:
            return None
        room = Room(id=event["room_id"], owner=az.id)
        self.log.debug(f"Registering {az.name} ({az.id}) as the owner of {room.id}")
        await room.insert()
        return room

    async def _collect_events(self, events: List[JSON], output: Dict[UUID, Events], ephemeral: bool
                              ) -> None:
        for event in events:
            RECEIVED_EVENTS.labels(type=event.get("type", "")).inc()
            room_id = event.get("room_id")
            if room_id:
                room = await Room.get(room_id)
                if not room and not ephemeral:
                    room = await self.register_room(event)
                if room:
                    output_array = output[room.owner].edu if ephemeral else output[room.owner].pdu
                    output_array.append(event)
                    output[room.owner].types.append(event.get("type", ""))
                else:
                    self.log.debug(f"No target found for event in {room_id}")
                    DROPPED_EVENTS.labels(type=event.get("type", "")).inc()
            # elif event.get("type") == "m.presence":
            #     TODO find all appservices that care about the sender's presence.
            #     pass

    async def _collect_otk_count(self, otk_count: Optional[Dict[UserID, DeviceOTKCount]],
                                 output: Dict[UUID, Events]) -> None:
        if not otk_count:
            return
        for user_id, otk_count in otk_count.items():
            az = await self._get_az_from_user_id(user_id)
            if az:
                # TODO metrics/logs for received OTK counts?
                output[az.id].otk_count[user_id] = otk_count

    async def _send_transactions(self, events: Dict[UUID, Events], synchronous_to: List[str]
                                 ) -> Dict[str, bool]:
        wait_for: Dict[UUID, Awaitable[bool]] = {}

        for appservice_id, events in events.items():
            appservice = await AppService.get(appservice_id)
            self.log.debug(f"Preparing to send {len(events.pdu)} PDUs and {len(events.edu)} EDUs "
                           f"from transaction {events.txn_id} to {appservice.name}")
            task = asyncio.create_task(self.post_events(appservice, events))
            if str(appservice.id) in synchronous_to:
                wait_for[appservice.id] = task

        output: Dict[str, bool] = {}
        if wait_for:
            for appservice_id, task in wait_for.items():
                output[str(appservice_id)] = await task
        return output

    async def handle_transaction(self, txn_id: str, *, events: List[JSON], extra_data: JSON,
                                 ephemeral: Optional[List[JSON]] = None,
                                 device_otk_count: Optional[Dict[UserID, DeviceOTKCount]] = None,
                                 device_lists: Optional[DeviceLists] = None) -> Dict[str, bool]:
        self.log.debug(f"Received transaction {txn_id} with {len(events)} PDUs "
                       f"and {len(ephemeral or [])} EDUs")
        data: Dict[UUID, Events] = defaultdict(lambda: Events(txn_id))

        await self._collect_events(events, output=data, ephemeral=False)
        await self._collect_events(ephemeral or [], output=data, ephemeral=True)
        await self._collect_otk_count(device_otk_count, output=data)
        # TODO on device list changes, send notification to all bridges
        # await self._collect_device_lists(device_lists, output=data)

        synchronous_to = extra_data.get("com.beeper.asmux.synchronous_to", [])
        return await self._send_transactions(data, synchronous_to)
