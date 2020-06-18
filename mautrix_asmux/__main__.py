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
import sys

from aiohttp import ClientSession, TCPConnector

from mautrix.util.program import Program
from mautrix.util.async_db import Database

from . import __version__
from .config import Config
from .server import MuxServer
from .database import Base, upgrade_table


class AppServiceMux(Program):
    module = "mautrix_asmux"
    name = "mautrix-asmux"
    version = __version__
    command = "python -m mautrix-asmux"
    description = "A Matrix application service proxy and multiplexer"

    config_class = Config

    config: Config
    server: MuxServer
    client: ClientSession
    database: Database

    def prepare_arg_parser(self) -> None:
        super().prepare_arg_parser()
        self.parser.add_argument("-g", "--generate-registration", action="store_true",
                                 help="generate registration and quit")
        self.parser.add_argument("-r", "--registration", type=str, default="registration.yaml",
                                 metavar="<path>",
                                 help="the path to save the generated registration to (not needed "
                                      "for running mautrix-asmux)")

    def preinit(self) -> None:
        super().preinit()
        if self.args.generate_registration:
            self.generate_registration()
            sys.exit(0)

    def generate_registration(self) -> None:
        self.config.generate_registration()
        self.config.save()
        print(f"Registration generated and saved to {self.config.registration_path}")

    def prepare(self) -> None:
        super().prepare()
        self.database = Database(url=self.config["mux.database"], upgrade_table=upgrade_table)
        Base.db = self.database
        self.client = self.loop.run_until_complete(self._create_client())
        self.server = MuxServer(self.config, http=self.client, loop=self.loop)

    async def _create_client(self) -> ClientSession:
        conn = TCPConnector(limit=0)
        return ClientSession(loop=self.loop, connector=conn)

    def prepare_config(self) -> None:
        self.config = self.config_class(self.args.config, self.args.registration,
                                        self.args.base_config)
        if self.args.generate_registration:
            self.config._check_tokens = False
        self.load_and_update_config()

    async def start(self) -> None:
        await self.database.start()
        await self.server.start()
        await super().start()


AppServiceMux().run()
