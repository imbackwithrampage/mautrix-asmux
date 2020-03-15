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
from typing import List, Dict, Any, Optional
import random
import string
import re

from mautrix.util.config import (BaseFileConfig, BaseValidatableConfig, ConfigUpdateHelper,
                                 ForbiddenDefault, yaml)


class Config(BaseFileConfig, BaseValidatableConfig):
    registration_path: str
    _registration: Optional[Dict]
    _check_tokens: bool

    def __init__(self, path: str, registration_path: str, base_path: str) -> None:
        super().__init__(path, base_path)
        self.registration_path = registration_path
        self._registration = None
        self._check_tokens = True

    def save(self) -> None:
        super().save()
        if self._registration and self.registration_path:
            with open(self.registration_path, "w") as stream:
                yaml.dump(self._registration, stream)

    @staticmethod
    def _new_token() -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=64))

    @property
    def forbidden_defaults(self) -> List[ForbiddenDefault]:
        return [
            ForbiddenDefault("homeserver.domain", "example.com"),
        ] + ([
            ForbiddenDefault("appservice.as_token",
                             "This value is generated when generating the registration",
                             "Did you forget to generate the registration?"),
            ForbiddenDefault("appservice.hs_token",
                             "This value is generated when generating the registration",
                             "Did you forget to generate the registration?"),
        ] if self._check_tokens else [])

    def do_update(self, helper: ConfigUpdateHelper) -> None:
        copy, _, _ = helper

        copy("homeserver.address")
        copy("homeserver.domain")

        copy("appservice.address")

        copy("appservice.id")
        copy("appservice.bot_username")
        copy("appservice.bot_displayname")
        copy("appservice.bot_avatar")

        copy("appservice.as_token")
        copy("appservice.hs_token")

        copy("mux.hostname")
        copy("mux.port")
        copy("mux.database")

        copy("logging")

    def generate_registration(self) -> None:
        prefix = re.escape(self["appservice.namespace.prefix"])
        exclusive = self["appservice.namespace.exclusive"]
        server_name = re.escape(self["homeserver.domain"])

        self["appservice.as_token"] = self._new_token()
        self["appservice.hs_token"] = self._new_token()

        self._registration = {
            "id": self["appservice.id"],
            "as_token": self["appservice.as_token"],
            "hs_token": self["appservice.hs_token"],
            "namespaces": {
                "users": [{
                    "regex": f"@{prefix}.+:{server_name}",
                    "exclusive": exclusive,
                }],
                "aliases": [{
                    "regex": f"#{prefix}.+:{server_name}",
                    "exclusive": exclusive,
                }]
            },
            "url": self["appservice.address"],
            "sender_localpart": self["appservice.bot_username"],
            "rate_limited": False
        }
