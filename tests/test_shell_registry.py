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

from unittest.mock import MagicMock

from sandbox_mcp.shell_registry import ShellRegistry


def test_open_shell():
    reg = ShellRegistry()
    mock_shell = MagicMock()
    mock_shell.state = "idle"
    mock_shell.purpose = None
    mock_shell.uptime = 0
    mock_shell.last_command = None
    shell_id = reg.open("dev", mock_shell, purpose="test")
    assert shell_id.startswith("sh_")
    assert shell_id in [s["shell_id"] for s in reg.list_shells()]


def test_get_shell():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.open("dev", mock_shell)
    assert reg.get(shell_id) is mock_shell


def test_close_shell():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.open("dev", mock_shell)
    reg.close(shell_id)
    assert shell_id not in [s["shell_id"] for s in reg.list_shells()]
    mock_shell.close.assert_called_once()


def test_list_shells_by_machine():
    reg = ShellRegistry()
    mock1 = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    mock2 = MagicMock(state="running", purpose="tests", uptime=0, last_command="pytest")
    reg.open("dev", mock1)
    reg.open("dev", mock2, purpose="tests")
    reg.open("db", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    dev_shells = reg.list_shells(machine="dev")
    assert len(dev_shells) == 2


def test_list_shells_terminated_hint():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="terminated", purpose=None, uptime=0, last_command=None)
    reg.open("dev", mock_shell)
    shells = reg.list_shells()
    assert shells[0]["status"] == "terminated"
    assert "hint" in shells[0]


def test_get_or_create_default():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.get_or_create_default("dev", lambda: mock_shell)
    assert shell_id.startswith("sh_")
    shells = reg.list_shells(machine="dev")
    assert next(s for s in shells if s["shell_id"] == shell_id)["is_default"] is True
    shell_id2 = reg.get_or_create_default("dev", lambda: MagicMock())
    assert shell_id == shell_id2


def test_set_default_shell():
    reg = ShellRegistry()
    shell1 = reg.open("dev", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    shell2 = reg.open("dev", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    machine = reg.set_default(shell2)
    assert machine == "dev"
    assert reg.get_machine(shell2) == "dev"
    shells = reg.list_shells(machine="dev")
    assert next(s for s in shells if s["shell_id"] == shell1)["is_default"] is False
    assert next(s for s in shells if s["shell_id"] == shell2)["is_default"] is True


def test_close_all_for_machine():
    reg = ShellRegistry()
    reg.open("dev", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    reg.open("dev", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    reg.open("db", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    reg.close_all_for_machine("dev")
    assert len(reg.list_shells(machine="dev")) == 0
    assert len(reg.list_shells()) == 1
