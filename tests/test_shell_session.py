import time

from sandbox_mcp.shell_session import ShellSession


def test_send_wait_true_simple_command():
    session = ShellSession(["bash"])
    result = session.send("echo hello world", wait=True, timeout=5)
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "hello world" in result["output"]
    session.close()


def test_send_wait_true_preserves_state():
    session = ShellSession(["bash"])
    session.send("export FOO=bar", wait=True, timeout=5)
    result = session.send("echo $FOO", wait=True, timeout=5)
    assert "bar" in result["output"]
    session.close()


def test_send_wait_true_exit_code():
    session = ShellSession(["bash"])
    result = session.send("exit 42", wait=True, timeout=5)
    assert result["status"] in ("completed", "terminated")
    session.close()


def test_send_wait_true_timeout_returns_running():
    session = ShellSession(["bash"])
    result = session.send("sleep 10", wait=True, timeout=1)
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
    session.send("sleep 5", wait=True, timeout=0.5)
    result = session.send("echo should_fail", wait=True, timeout=1)
    assert result["status"] == "error"
    assert "busy" in result.get("error", "").lower()
    session.close()


def test_read_after_wait_false():
    session = ShellSession(["bash"])
    session.send("echo hello; sleep 0.3; echo done", wait=False, timeout=3)
    time.sleep(1.0)
    found_completed = False
    for _ in range(10):
        result = session.read()
        if result["status"] == "completed":
            found_completed = True
            assert result["exit_code"] == 0
            break
        time.sleep(0.2)
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
    session = ShellSession(["bash"])
    result = session.send("seq 1 100000", wait=True, timeout=10, max_output=5000)
    assert result["status"] == "completed"
    assert "truncated" in result["output"].lower()
    assert "100000" in result["output"]
    session.close()
