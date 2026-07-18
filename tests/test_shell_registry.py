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
    monkeypatch.setattr("sandbox_mcp.shell_registry._health_check", lambda session: None)


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
    monkeypatch.setattr("sandbox_mcp.shell_registry._health_check", lambda session: None)
    reg = ShellRegistry()
    session = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.open("dev", session)
    assert shell_id in [s["shell_id"] for s in reg.list_shells()]


# ---------- get_or_create_default attaches previous_shell ----------


def _stub_session(
    *, state="idle", bash_pid=99999, exit_reason="unknown", last_exit_code=None, last_command=""
):
    """Build a minimal ShellSession-shaped object for registry tests.

    Bypasses real Popen so we don't need a real bash subprocess; the
    registry only touches ``state``, ``bash_pid``, ``exit_reason``,
    ``last_exit_code``, ``last_command``, and ``attach_previous_shell``.
    """
    from unittest.mock import MagicMock

    s = MagicMock()
    s.state = state
    s.bash_pid = bash_pid
    s.exit_reason = exit_reason
    s.last_exit_code = last_exit_code
    s.last_command = last_command
    s.attach_previous_shell = MagicMock()
    return s


def test_get_or_create_default_attaches_prev_on_self_heal(monkeypatch):
    """After self-heal, the new shell has the previous_shell info attached."""
    monkeypatch.setattr("sandbox_mcp.shell_registry._health_check", lambda session: None)

    reg = ShellRegistry()

    # First default: a dead shell with captured exit info.
    dead = _stub_session(
        state="terminated",
        bash_pid=11111,
        exit_reason="exit",
        last_exit_code=0,
        last_command="exit 0",
    )
    s1 = reg.get_or_create_default("dev", lambda: dead)
    assert s1 in [s["shell_id"] for s in reg.list_shells()]

    # Self-heal: factory returns a fresh stub session.
    fresh = _stub_session(bash_pid=22222)
    s2 = reg.get_or_create_default("dev", lambda: fresh)

    # The fresh session received attach_previous_shell with the dead
    # session's snapshot.
    fresh.attach_previous_shell.assert_called_once()
    captured = fresh.attach_previous_shell.call_args.args[0]
    assert captured["previous_bash_pid"] == 11111
    assert captured["last_command"] == "exit 0"
    assert captured["exit_reason"] == "exit"
    assert captured["exit_code"] == 0

    reg.close(s2)


def test_get_or_create_default_no_prev_when_factory_raises(monkeypatch):
    """If factory() raises, no prev is leaked (no session to attach to)."""
    monkeypatch.setattr("sandbox_mcp.shell_registry._health_check", lambda session: None)

    reg = ShellRegistry()
    dead = _stub_session(state="terminated", bash_pid=11111, exit_reason="exit")
    reg.get_or_create_default("dev", lambda: dead)

    def bad_factory():
        raise RuntimeError("docker daemon down")

    with pytest.raises(RuntimeError):
        reg.get_or_create_default("dev", bad_factory)
    # Old shell is gone (closed in the self-heal path); new wasn't created.
    assert reg.list_shells() == []


def test_get_or_create_default_no_prev_when_open_health_fails(monkeypatch):
    """If open()'s health check fails, prev is not attached to anything."""
    from sandbox_mcp.shell_session import ShellUnhealthy

    call_count = {"n": 0}

    def fake_health_check(session):
        call_count["n"] += 1
        # First call: pass (register the dead seed).
        # Second call: raise (the new shell is broken).
        if call_count["n"] == 2:
            raise ShellUnhealthy("broken")

    monkeypatch.setattr("sandbox_mcp.shell_registry._health_check", fake_health_check)

    reg = ShellRegistry()
    dead = _stub_session(state="terminated", bash_pid=11111, exit_reason="exit")
    reg.get_or_create_default("dev", lambda: dead)

    fresh = _stub_session(bash_pid=22222)
    with pytest.raises(ShellUnhealthy):
        reg.get_or_create_default("dev", lambda: fresh)

    # open()'s cleanup closed the fresh session; it never got attached.
    fresh.attach_previous_shell.assert_not_called()
    fresh.close.assert_called_once()


def test_get_or_create_default_existing_alive_no_attach(monkeypatch):
    """When the default is still alive, no new session is created and
    nothing is attached — agents shouldn't see previous_shell on a
    non-restart."""
    monkeypatch.setattr("sandbox_mcp.shell_registry._health_check", lambda session: None)

    reg = ShellRegistry()
    alive = _stub_session(state="idle", bash_pid=11111)
    s1 = reg.get_or_create_default("dev", lambda: alive)

    # Second call: same default, still alive.
    again = reg.get_or_create_default("dev", lambda: _stub_session(bash_pid=22222))
    assert again == s1
    # Neither session had attach_previous_shell called.
    alive.attach_previous_shell.assert_not_called()
    reg.close(s1)
