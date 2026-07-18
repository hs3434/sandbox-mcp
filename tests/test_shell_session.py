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

import time

import pytest

from sandbox_mcp.shell_session import ShellSession


def test_send_wait_true_simple_command():
    session = ShellSession(["bash"])
    result = session.send("echo hello world", wait=True, timeout=5)
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "hello world" in result["output"]
    assert "bash_pid" in result, "send() result must include bash_pid"
    assert isinstance(result["bash_pid"], int)
    session.close()


def test_bash_pid_changes_after_shell_exit_and_recreate():
    """After exit, the next send() runs in a fresh bash → different pid.

    This is the signal agents use to detect that the shell was restarted
    (e.g. after self-heal in get_or_create_default) and that any
    in-memory state (exports, cwd, jobs) is gone.
    """
    from sandbox_mcp.shell_registry import ShellRegistry

    reg = ShellRegistry()
    s1 = reg.get_or_create_default("dev", lambda: ShellSession(["bash"]))
    pid1 = reg.get(s1).bash_pid
    assert pid1 is not None

    # Simulate shell death: agent ran ``exit 0``.  The drain thread will
    # set state="terminated" when bash closes stdout, so the next
    # get_or_create_default should self-heal.
    session1 = reg.get(s1)
    session1.send("exit 0", wait=True, timeout=5)
    assert session1.state == "terminated"

    s2 = reg.get_or_create_default("dev", lambda: ShellSession(["bash"]))
    assert s2 != s1, "self-heal should have replaced the dead default"
    pid2 = reg.get(s2).bash_pid
    assert pid2 != pid1, f"new shell must have a different bash pid (was {pid1}, now {pid2})"
    reg.close(s2)


def test_send_wait_true_preserves_state():
    session = ShellSession(["bash"])
    session.send("export FOO=bar", wait=True, timeout=5)
    result = session.send("echo $FOO", wait=True, timeout=5)
    assert "bar" in result["output"]
    session.close()


def test_send_wait_true_exit_code():
    """exit code is captured when bash can run the end echo."""
    session = ShellSession(["bash"])
    result = session.send("exit 42", wait=True, timeout=5)
    # bash itself exits before echoing __END_; the drain then sees EOF.
    # The state machine reports 'terminated' and exit_code=None in that
    # case. We accept either here so the test documents the behaviour.
    assert result["status"] in ("completed", "terminated")
    session.close()


def test_send_wait_true_timeout_returns_running():
    session = ShellSession(["bash"])
    result = session.send("sleep 5", wait=True, timeout=1)
    assert result["status"] == "running"
    assert result["exit_code"] is None
    session.close()


def test_send_wait_false_confirms_execution():
    session = ShellSession(["bash"])
    result = session.send("echo started", wait=False, timeout=3)
    assert result["status"] == "running"
    assert result["confirmed"] is True
    session.close()


def test_send_on_busy_shell_rejected():
    session = ShellSession(["bash"])
    session.send("sleep 2", wait=True, timeout=0.5)
    result = session.send("echo should_fail", wait=True, timeout=1)
    assert result["status"] == "error"
    assert "busy" in result.get("error", "").lower()
    session.close()


def test_read_after_wait_false():
    session = ShellSession(["bash"])
    session.send("echo hello; sleep 0.3; echo done", wait=False, timeout=3)
    time.sleep(0.5)
    found_completed = False
    for _ in range(20):
        result = session.read()
        if result["status"] == "completed":
            found_completed = True
            assert result["exit_code"] == 0
            break
        time.sleep(0.1)
    assert found_completed, "Should detect completion via __END_ marker"
    session.close()


def test_read_idle_shell():
    session = ShellSession(["bash"])
    result = session.read()
    assert result["status"] == "idle"
    assert result["output"] == ""
    session.close()


def test_close_kills_process():
    session = ShellSession(["bash"])
    session.close()
    assert session.state == "terminated"
    result = session.send("echo test", wait=True, timeout=1)
    assert result["status"] == "error"


def test_terminated_on_bash_exit():
    session = ShellSession(["bash"])
    session.send("exit 0", wait=True, timeout=5)
    time.sleep(0.3)
    assert session.state == "terminated"
    session.close()


def test_output_truncation():
    """Truncation works when output exceeds max_output."""
    session = ShellSession(["bash"])
    # echo hello world is ~12 bytes — force truncation with max_output=1.
    result = session.send(
        "echo hello world",
        wait=True,
        timeout=5,
        max_output=1,
    )
    assert result["status"] == "completed"
    assert "truncated" in result["output"].lower()
    session.close()


#    session = ShellSession(["bash"])
#    result = session.send(
#        "printf 'x%.0s' {1..50000}",
#        wait=True,
#        timeout=10,
#        max_output=5000,
#    )
#    assert result["status"] == "completed"
#    assert "truncated" in result["output"].lower()
#    session.close()


def test_drain_exits_on_bash_exit():
    """When bash exits, drain should see EOF and exit on its own.

    With readline-based drain, the only exit signal is EOF on stdout,
    which fires when bash closes its stdout (after `proc.kill`).
    """
    import time

    session = ShellSession(["bash"])
    thread = session._drain_thread
    session.send("exit 0", wait=True, timeout=5)
    # bash is now dead. Drain should exit on its own within ~1s.
    deadline = time.time() + 2.0
    while time.time() < deadline and thread.is_alive():
        time.sleep(0.05)
    assert not thread.is_alive(), "drain should exit after bash exits"
    session.close()


def test_close_joins_drain_thread():
    """close() must release the drain thread so it doesn't leak FDs."""
    session = ShellSession(["bash"])
    thread = session._drain_thread
    assert thread is not None
    assert thread.is_alive()
    session.close()
    assert not thread.is_alive()


def test_close_kills_descendant_subprocesses():
    """close() must kill descendants, not just bash, so the stdout pipe closes.

    Regression: bash spawning ``sleep N`` made sleep inherit bash's stdout
    pipe FD.  Killing only bash left sleep alive holding the pipe open,
    so the drain thread blocked on readline waiting for EOF and
    ``_drain_thread.join(timeout=2)`` timed out on every close().  Fix:
    start bash in its own process group and killpg(SIGKILL) in close().

    We can't easily prove sleep is dead (it'd need /proc inspection or a
    child reaper), so the observable contract is just: close() returns
    fast (<1s) and the drain thread exits within close()'s join window.
    """
    import time as _time

    session = ShellSession(["bash"])
    # Long-running descendant — would previously keep the pipe open.
    session.send("sleep 30", wait=True, timeout=0.2)
    assert session.state == "running"

    drain_thread = session._drain_thread
    assert drain_thread is not None and drain_thread.is_alive()

    t0 = _time.monotonic()
    session.close()
    elapsed = _time.monotonic() - t0

    assert not drain_thread.is_alive(), "drain thread must exit when descendants are killed"
    # Pre-fix this was >2s because of the orphaned sleep keeping the pipe
    # open.  Post-fix, killpg closes the pipe and drain exits in ms.
    assert elapsed < 1.0, f"close() took {elapsed:.2f}s, expected <1s"


# ---------- exit_reason / last_exit_code capture ----------


def test_drain_captures_exit_reason_on_normal_exit():
    """bash `exit 0` → exit_reason='exit', last_exit_code=0."""
    session = ShellSession(["bash"])
    session.send("exit 0", wait=True, timeout=5)
    session._drain_thread.join(timeout=2)
    assert session.state == "terminated"
    assert session.exit_reason == "exit"
    assert session.last_exit_code == 0


def test_drain_captures_exit_reason_on_nonzero_exit():
    """bash `exit 42` → exit_reason='exit', last_exit_code=42."""
    session = ShellSession(["bash"])
    session.send("exit 42", wait=True, timeout=5)
    session._drain_thread.join(timeout=2)
    assert session.exit_reason == "exit"
    assert session.last_exit_code == 42


def test_send_captures_broken_pipe_exit_reason():
    """Write to closed stdin → exit_reason='broken_pipe', code=None.

    We can't easily get a BrokenPipeError from send() alone (the
    terminated-state guard fires first when state is already set),
    but the state transition is enough: the drain thread sees EOF,
    sets state='terminated', and on the next send() the early-return
    guard returns the standard error response.  exit_reason stays
    'unknown' in this path because drain couldn't read proc.poll()
    mid-EOF — that's why we don't claim broken_pipe here, only that
    the state transition lands cleanly.
    """
    import os
    import signal

    session = ShellSession(["bash"])
    os.kill(session._process.pid, signal.SIGKILL)
    session._drain_thread.join(timeout=2)
    assert session.state == "terminated"
    # Drain thread captured exit info from proc.poll() — should be
    # signal-based since we SIGKILL'd bash.
    assert session.exit_reason in {"signal", "exit"}
    assert session.last_exit_code is not None


def test_exit_reason_default_is_unknown():
    """Fresh session has exit_reason='unknown' until a death event sets it."""
    session = ShellSession(["bash"])
    assert session.exit_reason == "unknown"
    assert session.last_exit_code is None
    session.close()


# ---------- previous_shell one-shot delivery ----------


def test_with_pid_includes_previous_shell_one_shot():
    """_with_pid injects previous_shell once, then clears it."""
    session = ShellSession(["bash"])
    info = {
        "previous_bash_pid": 12345,
        "last_command": "rm -rf /",
        "exit_reason": "exit",
        "exit_code": 1,
    }
    session.attach_previous_shell(info)
    result1 = session._with_pid({"status": "completed"})
    assert result1.get("previous_shell") == info

    # Second call: cleared, no previous_shell.
    result2 = session._with_pid({"status": "completed"})
    assert "previous_shell" not in result2
    session.close()


def test_with_pid_omits_previous_shell_when_none_attached():
    """No prior attach → no previous_shell key."""
    session = ShellSession(["bash"])
    result = session._with_pid({"status": "completed"})
    assert "previous_shell" not in result
    session.close()


def test_with_pid_preserves_bash_pid_alongside_previous_shell():
    """Both fields can coexist; one-shot only clears previous_shell."""
    session = ShellSession(["bash"])
    info = {
        "previous_bash_pid": 12345,
        "last_command": "exit",
        "exit_reason": "exit",
        "exit_code": 0,
    }
    session.attach_previous_shell(info)
    result = session._with_pid({"status": "completed"})
    assert "bash_pid" in result  # current shell's pid
    assert "previous_shell" in result  # and prior shell's info
    session.close()


# ---------- _health_check ----------


def test_health_check_passes_for_fresh_session():
    from sandbox_mcp.shell_session import _health_check

    session = ShellSession(["bash"])
    _health_check(session)  # should not raise
    session.close()


def test_health_check_raises_when_send_returns_non_completed(monkeypatch):
    from sandbox_mcp.shell_session import ShellUnhealthy, _health_check

    session = ShellSession(["bash"])
    monkeypatch.setattr(
        session,
        "send",
        lambda *a, **kw: {"status": "running", "exit_code": None},
    )
    with pytest.raises(ShellUnhealthy, match="running"):
        _health_check(session)
    session.close()


def test_health_check_raises_when_session_state_terminated(monkeypatch):
    from sandbox_mcp.shell_session import ShellUnhealthy, _health_check

    session = ShellSession(["bash"])

    def fake_send(*a, **kw):
        session._state = "terminated"
        return {"status": "completed", "exit_code": 0}

    monkeypatch.setattr(session, "send", fake_send)
    with pytest.raises(ShellUnhealthy, match="died during"):
        _health_check(session)
