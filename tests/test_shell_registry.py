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

import pytest

from sandbox_mcp.shell_registry import ShellRegistry


@pytest.fixture(autouse=True)
def _patch_health_check(monkeypatch):
    """Existing tests don't exercise health checking; bypass it so
    MagicMock sessions can register without raising.
    """
    monkeypatch.setattr(
        "sandbox_mcp.shell_registry._health_check", lambda session: None
    )


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


def test_get_or_create_default_replaces_dead_shell():
    """Dead default shell is dropped and a fresh one is created.

    Without self-heal, an agent that ran ``exit`` (or hit a shell crash)
    would get ``"Shell is terminated"`` on every subsequent
    ``shell_exec`` until it noticed and called ``shell_remove``.  The
    registry now detects state=="terminated" on the cached default and
    transparently recreates.
    """
    reg = ShellRegistry()
    dead_shell = MagicMock(state="terminated", purpose=None, uptime=0, last_command=None)
    dead_id = reg.get_or_create_default("dev", lambda: dead_shell)

    fresh_shell = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    fresh_id = reg.get_or_create_default("dev", lambda: fresh_shell)

    assert fresh_id != dead_id, "dead default must be replaced, not reused"
    # Old shell was closed (self-heal calls reg.close() on the dead one).
    dead_shell.close.assert_called_once()
    # New shell is the active default.
    assert reg.get_machine(fresh_id) == "dev"
    shells = reg.list_shells(machine="dev")
    assert next(s for s in shells if s["shell_id"] == fresh_id)["is_default"] is True


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


# ---------- open() health check ----------


def test_open_health_checks_before_publishing(monkeypatch):
    """open() runs _health_check; on failure closes the session and raises."""
    from sandbox_mcp.shell_session import ShellUnhealthy

    monkeypatch.setattr(
        "sandbox_mcp.shell_registry._health_check",
        lambda session: (_ for _ in ()).throw(ShellUnhealthy("broken")),
    )

    reg = ShellRegistry()
    broken_session = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)

    with pytest.raises(ShellUnhealthy):
        reg.open("dev", broken_session)

    broken_session.close.assert_called_once()
    assert reg.list_shells() == []


def test_open_publishes_healthy_session(monkeypatch):
    monkeypatch.setattr(
        "sandbox_mcp.shell_registry._health_check", lambda session: None
    )
    reg = ShellRegistry()
    session = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.open("dev", session)
    assert shell_id in [s["shell_id"] for s in reg.list_shells()]
