# mautrix-asmux - A Matrix application service proxy and multiplexer
# Copyright (C) 2021 Beeper, Inc. All rights reserved.
from __future__ import annotations

from typing import ClassVar, Iterable, cast
from uuid import UUID, uuid4
import base64
import hashlib
import hmac
import math
import secrets
import time

from attr import dataclass

from mautrix.util.async_db import Connection

from ...sygnal import PushKey
from .base import Base
from .user import User


@dataclass
class AppService(Base):
    id: UUID
    owner: str
    prefix: str

    bot: str
    address: str
    hs_token: str
    as_token: str
    push: bool
    config_password_hash: bytes | None = None
    config_password_expiry: int | None = None
    push_key: PushKey | None = None

    login_token: str | None = None
    created_: bool = False

    cache_by_id: ClassVar[dict[UUID, AppService]] = {}
    cache_by_owner: ClassVar[dict[tuple[str, str], AppService]] = {}

    @classmethod
    def empty_cache(cls) -> None:
        cls.cache_by_id = {}
        cls.cache_by_owner = {}

    def __attrs_post_init__(self) -> None:
        if self.push_key and isinstance(self.push_key, str):
            self.push_key = PushKey.parse_json(self.push_key)

    @property
    def name(self) -> str:
        return f"{self.owner}/{self.prefix}"

    @property
    def real_as_token(self) -> str:
        return f"{self.id}-{self.as_token}"

    def _add_to_cache(self) -> AppService:
        self.cache_by_id[self.id] = self
        self.cache_by_owner[(self.owner, self.prefix)] = self
        return self

    def _delete_from_cache(self) -> None:
        del self.cache_by_id[self.id]
        del self.cache_by_owner[(self.owner, self.prefix)]

    @classmethod
    async def get(cls, az_id: UUID, *, conn: Connection | None = None) -> AppService | None:
        try:
            return cls.cache_by_id[az_id]
        except KeyError:
            pass
        row = await (conn or cls.db).fetchrow(
            "SELECT appservice.id, owner, prefix, bot, address, "
            '       hs_token, as_token, push, "user".login_token, '
            "       config_password_hash, config_password_expiry, push_key "
            'FROM appservice JOIN "user" ON "user".id=appservice.owner '
            "WHERE appservice.id=$1::uuid",
            az_id,
        )
        return AppService(**cast(dict, row))._add_to_cache() if row else None

    @classmethod
    async def find(
        cls, owner: str, prefix: str, *, conn: Connection | None = None
    ) -> AppService | None:
        try:
            return cls.cache_by_owner[(owner, prefix)]
        except KeyError:
            pass
        row = await (conn or cls.db).fetchrow(
            "SELECT appservice.id, owner, prefix, bot, address, hs_token, "
            '       as_token, push, "user".login_token, '
            "       config_password_hash, config_password_expiry, push_key "
            'FROM appservice JOIN "user" ON "user".id=appservice.owner '
            "WHERE owner=$1 AND prefix=$2 ",
            owner,
            prefix,
        )
        return AppService(**cast(dict, row))._add_to_cache() if row else None

    @classmethod
    async def find_or_create(
        cls, user: User, prefix: str, *, bot: str = "bot", address: str = "", push: bool = True
    ) -> AppService:
        try:
            return cls.cache_by_owner[(user.id, prefix)]
        except KeyError:
            pass
        async with cls.db.acquire() as conn, conn.transaction():
            az = await cls.find(user.id, prefix, conn=conn)
            if not az:
                uuid = uuid4()
                hs_token = secrets.token_urlsafe(48)
                # The input AS token also contains the UUID, so we want this to be a bit shorter
                as_token = secrets.token_urlsafe(20)
                az = AppService(
                    id=uuid,
                    owner=user.id,
                    prefix=prefix,
                    bot=bot,
                    address=address,
                    hs_token=hs_token,
                    as_token=as_token,
                    push=push,
                    push_key=None,
                    login_token=user.login_token,
                )
                az.created_ = True
                await az.insert(conn=conn)
            return az

    @classmethod
    async def get_many(
        cls, ids: list[UUID], *, conn: Connection | None = None
    ) -> Iterable[AppService]:
        rows = await (conn or cls.db).fetch(
            "SELECT appservice.id, owner, prefix, bot, address, hs_token,"
            '       as_token, push, "user".login_token, '
            "       config_password_hash, config_password_expiry, push_key "
            'FROM appservice JOIN "user" ON "user".id=appservice.owner '
            "WHERE appservice.id = ANY($1::uuid[])",
            ids,
        )
        return (AppService(**row)._add_to_cache() for row in rows)

    async def insert(self, *, conn: Connection | None = None) -> None:
        await (conn or self.db).execute(
            "INSERT INTO appservice "
            "(id, owner, prefix, bot, address, hs_token, as_token, push) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            self.id,
            self.owner,
            self.prefix,
            self.bot,
            self.address,
            self.hs_token,
            self.as_token,
            self.push,
        )
        self._add_to_cache()

    async def set_address(self, address: str, *, conn: Connection | None = None) -> bool:
        if address is None or self.address == address:
            return False
        self.address = address
        await (conn or self.db).execute(
            "UPDATE appservice SET address=$2 WHERE id=$1", self.id, self.address
        )
        return True

    async def set_push(self, push: bool) -> None:
        if push is None or push == self.push:
            return
        self.push = push
        await self.db.execute("UPDATE appservice SET push=$2 WHERE id=$1", self.id, self.push)

    async def generate_password(self, lifetime: int | None = None) -> str:
        token = secrets.token_bytes()
        self.config_password_hash = hashlib.sha256(token).digest()
        self.config_password_expiry = None if lifetime is None else (int(time.time()) + lifetime)
        await self.db.execute(
            "UPDATE appservice SET config_password_hash=$2, "
            "                      config_password_expiry=$3 "
            "WHERE id=$1",
            self.id,
            self.config_password_hash,
            self.config_password_expiry,
        )
        # We use case-insensitive base32 instead of base64 due to
        # https://gitlab.com/beeper/brooklyn/-/issues/7
        return base64.b32encode(token).decode("utf-8").rstrip("=")

    def check_password(self, password: str) -> bool:
        assert self.config_password_hash is not None
        pad_length = math.ceil(len(password) / 8) * 8 - len(password)
        padded_password = password.upper() + "=" * pad_length
        hashed_password = hashlib.sha256(base64.b32decode(padded_password)).digest()
        correct = hmac.compare_digest(hashed_password, self.config_password_hash)
        expired = (
            self.config_password_expiry < int(time.time())
            if self.config_password_expiry is not None
            else False
        )
        return correct and not expired

    async def set_push_key(self, push_key: PushKey | None) -> None:
        if push_key and not push_key.pushkey:
            push_key = None
        self.push_key = push_key
        await self.db.execute(
            "UPDATE appservice SET push_key=$2 WHERE id=$1",
            self.id,
            self.push_key.json() if self.push_key else None,
        )

    async def delete(self, *, conn: Connection | None = None) -> None:
        self._delete_from_cache()
        await (conn or self.db).execute("DELETE FROM appservice WHERE id=$1", self.id)
