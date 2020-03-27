# mautrix-asmux - A Matrix application service proxy and multiplexer
# Copyright (C) 2020 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from asyncpg import Connection

from .upgrade_table import upgrade_table


@upgrade_table.register(description="Initial revision")
async def upgrade_v1(conn: Connection) -> None:
    await conn.execute("""CREATE TABLE "user"(
        id          VARCHAR(32) PRIMARY KEY,
        login_token VARCHAR(255) NOT NULL
    )""")
    await conn.execute("""CREATE TABLE appservice (
        id     UUID         PRIMARY KEY,
        owner  VARCHAR(32)  NOT NULL REFERENCES "user"(id),
        prefix VARCHAR(32)  NOT NULL,

        bot      VARCHAR(32)  NOT NULL,
        address  VARCHAR(255) NOT NULL,
        hs_token VARCHAR(255) NOT NULL,
        as_token VARCHAR(255) NOT NULL,

        UNIQUE (owner, prefix)
    )""")
    await conn.execute("""CREATE TABLE room (
        id    VARCHAR(255) PRIMARY KEY,
        owner UUID         REFERENCES appservice(id)
    )""")