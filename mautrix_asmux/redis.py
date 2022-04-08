from typing import cast
from uuid import UUID
import asyncio
import logging

from aioredis import Redis

from mautrix.types import RoomID
from mautrix_asmux.database.table import AppService, Room, User

AS_CACHE_CHANNEL = "appservice-cache-invalidation"
ROOM_CACHE_CHANNEL = "room-cache-invalidation"
USER_CACHE_CHANNEL = "user-cache-invalidation"


class RedisCacheHandler:
    log: logging.Logger = logging.getLogger("mau.redis")

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        self.pubsub.subscribe(
            **{
                AS_CACHE_CHANNEL: self.handle_invalidate_as,
                ROOM_CACHE_CHANNEL: self.handle_invalidate_room,
                USER_CACHE_CHANNEL: self.handle_invalidate_user,
            }
        )

        asyncio.create_task(self.read_pubsub_messages())

    # Listen for and handle invalidation messages

    async def read_pubsub_messages(self):
        while True:
            try:
                for message in await self.pubsub.listen():
                    self.log.warning(f"Unexpected redis pubsub message: {message}")
            except Exception as e:
                self.log.critical(f"Redis failure, throwing caches: {e}")
                AppService.empty_cache()
                Room.empty_cache()
                User.empty_cache()
            asycio.sleep(1)

    async def handle_invalidate_as(self, message: bytes):
        az = await AppService.get(UUID(message.decode()))
        if az:
            az._delete_from_cache()

    async def handle_invalidate_room(self, message: bytes):
        room = await Room.get(RoomID(message.decode()))
        if room:
            room._delete_from_cache()

    async def handle_invalidate_user(self, message: bytes):
        user = await User.get(message.decode())
        if user:
            user._delete_from_cache()

    # Publish invalidation messages

    async def invalidate_az(self, az: AppService) -> None:
        await self.redis.publish(AS_CACHE_CHANNEL, cast(str, az.id))

    async def invalidate_room(self, room: Room) -> None:
        await self.redis.publish(ROOM_CACHE_CHANNEL, room.id)

    async def invalidate_user(self, user: User) -> None:
        await self.redis.publish(USER_CACHE_CHANNEL, user.id)
