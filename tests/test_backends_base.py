# sandbox-mcp - Sandbox Environment Manager MCP server
# Copyright (C) 2024  Sandbox MCP Contributors
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

import pytest

from sandbox_mcp.backends.base import Backend, TargetInfo


def test_target_info_dataclass():
    info = TargetInfo(name="dev", backend="docker", status="running", purpose="Dev")
    assert info.name == "dev"
    assert info.backend == "docker"
    assert info.status == "running"
    assert info.purpose == "Dev"


def test_backend_is_abstract():
    with pytest.raises(TypeError):
        Backend()
