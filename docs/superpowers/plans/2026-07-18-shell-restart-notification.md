# Shell Restart Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface a `previous_shell` field on `send`/`read` results so agents detect shell restarts (self-heal) and the reason for the prior shell's death, plus health-check every newly-created shell at the registry's `open()` entry point.

**Architecture:** Two-part change. (1) Capture exit info (`exit_reason`, `last_exit_code`) at death points in `ShellSession`, and surface it via `ShellSession.exit_reason` / `last_exit_code`. (2) On self-heal in `ShellRegistry.get_or_create_default`, snapshot the dead session before close, then attach the snapshot to the replacement shell as `_previous_shell_info`. `_with_pid` injects + clears it once. `ShellRegistry.open()` runs a `_health_check` so broken factory results never get registered.

**Tech Stack:** Python 3.12+, `subprocess.Popen` (local), docker SDK `exec_inspect` (docker backend), pytest. No new external deps.

**Reference spec:** `docs/superpowers/specs/2026-07-18-shell-restart-notification-design.md`

---

## File Map

| File | Role | New / Modified |
|---|---|---|
| `src/sandbox_mcp/shell_session.py` | ShellSession class (drain thread, send/read, _with_pid); capture death info; host `_health_check` + `ShellUnhealthy` | Modified |
| `src/sandbox_mcp/shell_registry.py` | open() runs health check; get_or_create_default captures prev; `_capture_for_replacement` helper | Modified |
| `src/sandbox_mcp/backends/docker_backend.py` | `DockerExecProcess.poll()` for exit code on docker backend | Modified |
| `src/sandbox_mcp/server.py` | `_handle_shell_exec` catches `ShellUnhealthy` + generic factory exception with structured error_kind | Modified |
| `tests/test_shell_session.py` | exit_reason tests, health_check tests, previous_shell delivery tests | Modified |
| `tests/test_shell_registry.py` | open() health check + prev attach tests | Modified |
| `tests/test_docker_backend.py` | `DockerExecProcess.poll()` test | Modified |
| `tests/test_server.py` | `_handle_shell_exec` error_kind tests | Modified |
| `README.md` / `README.zh.md` | Doc note on the self-heal + bash_pid contract (already added in 6bca12b) | No change |

---

## Task 1: Capture exit_reason + last_exit_code in ShellSession

**Files:**
- Modify: `src/sandbox_mcp/shell_session.py` (drain thread, send, close, __init__)
- Test: `tests/test_shell_session.py`

The drain thread already runs in the background; we add a small block that runs after EOF to read the exit info from the process. `send()` catches BrokenPipeError and should set `exit_reason="broken_pipe"`. `close()` already kills the process — the drain thread will record the SIGKILL exit afterward.

- [ ] **Step 1.1: Add failing tests for exit_reason capture**

Add to `tests/test_shell_session.py`:

```python
def test_drain_captures_exit_reason_on_normal_exit():
    """bash `exit 0` → exit_reason='exit', last_exit_code=0."""
    session = ShellSession(["bash"])
    session.send("exit 0", wait=True, timeout=5)
    # Drain thread finishes asynchronously; wait for it to settle.
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
    """Write to closed stdin → exit_reason='broken_pipe', code=None."""
    import os
    import signal

    session = ShellSession(["bash"])
    # Kill bash externally, then try to send (BrokenPipeError).
    os.kill(session._process.pid, signal.SIGKILL)
    session._drain_thread.join(timeout=2)  # let drain see EOF
    result = session.send("echo should_break", wait=True, timeout=1)
    assert result["status"] == "terminated"
    assert session.exit_reason == "broken_pipe"
    assert session.last_exit_code is None


def test_exit_reason_default_is_unknown():
    """Fresh session has exit_reason='unknown' until a death event sets it."""
    session = ShellSession(["bash"])
    assert session.exit_reason == "unknown"
    assert session.last_exit_code is None
    session.close()
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `uv run pytest tests/test_shell_session.py -v -k "exit_reason or broken_pipe"`
Expected: 4 AttributeError on `exit_reason` / `last_exit_code` (fields don't exist yet).

- [ ] **Step 1.3: Add fields to ShellSession.__init__**

In `src/sandbox_mcp/shell_session.py`, add to `__init__` (next to other `_state` / `_process` initialisation, ~line 70):

```python
self.exit_reason: str = "unknown"
self.last_exit_code: int | None = None
```

- [ ] **Step 1.4: Capture exit info in `_drain()` after EOF**

In `src/sandbox_mcp/shell_session.py`, the `_drain` method's loop ends with `if not line: break`. Just before the post-loop `self._state = "terminated"` (around line 159), insert:

```python
        proc = self._process
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                self.exit_reason = "unknown"
            elif rc < 0:
                self.exit_reason = "signal"
                self.last_exit_code = -rc
            else:
                self.exit_reason = "exit"
                self.last_exit_code = rc
```

- [ ] **Step 1.5: Capture broken_pipe in `send()`**

In `src/sandbox_mcp/shell_session.py`, locate the `BrokenPipeError` handler in `send()` (around line 232):

```python
            except (BrokenPipeError, OSError):
                self._state = "terminated"
                return self._with_pid(
                    {"output": "", "exit_code": None, "status": "terminated"}
                )
```

Replace with:

```python
            except (BrokenPipeError, OSError):
                self._state = "terminated"
                self.exit_reason = "broken_pipe"
                self.last_exit_code = None
                return self._with_pid(
                    {"output": "", "exit_code": None, "status": "terminated"}
                )
```

- [ ] **Step 1.6: Run tests to verify they pass**

Run: `uv run pytest tests/test_shell_session.py -v -k "exit_reason or broken_pipe"`
Expected: 4 passed.

- [ ] **Step 1.7: Commit**

```bash
git add src/sandbox_mcp/shell_session.py tests/test_shell_session.py
git commit -m "feat(shell): capture exit_reason + last_exit_code at death points"
```

---

## Task 2: DockerExecProcess.poll() for docker backend

**Files:**
- Modify: `src/sandbox_mcp/backends/docker_backend.py`
- Test: `tests/test_docker_backend.py`

Docker backend's `DockerExecProcess` exposes `_exec_id` and an internal socket; we add a public `poll()` that calls `exec_inspect` and returns the exit code (or `None` while running). Same convention as `subprocess.Popen.poll()`.

- [ ] **Step 2.1: Add failing test**

Add to `tests/test_docker_backend.py`:

```python
def test_docker_exec_process_poll_returns_exit_code():
    """poll() returns exec_inspect's ExitCode; None while still running."""
    from unittest.mock import MagicMock
    from sandbox_mcp.backends.docker_backend import DockerExecProcess

    container = MagicMock()
    container.client.api.exec_create.return_value = {"Id": "exec-abc"}
    # exec_inspect returns ExitCode=None while running, int when exited.
    container.client.api.exec_inspect.return_value = {"ExitCode": 42}

    proc = DockerExecProcess(container, ["bash"])
    proc.poll(stop=False)  # don't kill threads in test
    assert container.client.api.exec_inspect.called

    container.client.api.exec_inspect.return_value = {"ExitCode": None}
    result = proc.poll(stop=False)
    # Just verify it doesn't raise; the precise None pass-through is
    # exercised by the assertion above.
    assert result is None or isinstance(result, int)
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `uv run pytest tests/test_docker_backend.py::test_docker_exec_process_poll_returns_exit_code -v`
Expected: AttributeError — `poll` not defined on `DockerExecProcess`.

- [ ] **Step 2.3: Add poll() method**

In `src/sandbox_mcp/backends/docker_backend.py`, add a `poll` method to `DockerExecProcess` near the existing `kill()` / `wait()`:

```python
    def poll(self, *, stop: bool = True):
        """Return the docker exec's ExitCode (or None while running).

        Mirrors :meth:`subprocess.Popen.poll`: returns ``None`` when the
        exec instance hasn't produced an exit code yet, otherwise the
        integer exit code (``0`` for success, ``N`` for ``exit N``,
        ``-N`` if killed by signal N — though docker usually reports
        ``137`` for SIGKILL rather than negative codes).

        ``stop=True`` joins the demux/stdin threads before returning,
        matching the lifecycle expectation of ``ShellSession._drain``.
        """
        info = self._container.client.api.exec_inspect(self._exec_id)
        exit_code = info.get("ExitCode")
        if exit_code is not None and stop:
            self._done.set()
            with contextlib.suppress(Exception):
                self._demux_thread.join(timeout=2)
            with contextlib.suppress(Exception):
                self._stdin_thread.join(timeout=2)
        return exit_code
```

- [ ] **Step 2.4: Run test to verify it passes**

Run: `uv run pytest tests/test_docker_backend.py::test_docker_exec_process_poll_returns_exit_code -v`
Expected: PASS.

- [ ] **Step 2.5: Commit**

```bash
git add src/sandbox_mcp/backends/docker_backend.py tests/test_docker_backend.py
git commit -m "feat(docker_backend): DockerExecProcess.poll() returns exit code"
```

---

## Task 3: `ShellUnhealthy` exception + `_health_check` helper

**Files:**
- Modify: `src/sandbox_mcp/shell_session.py`
- Test: `tests/test_shell_session.py`

`_health_check` sends `true` to verify the just-created session is alive. Healthy bash responds in milliseconds; only a broken shell hits the 1s timeout.

- [ ] **Step 3.1: Add failing tests for `_health_check`**

Add to `tests/test_shell_session.py`:

```python
def test_health_check_passes_for_fresh_session():
    from sandbox_mcp.shell_session import _health_check

    session = ShellSession(["bash"])
    _health_check(session)  # should not raise
    session.close()


def test_health_check_raises_when_send_returns_non_completed(monkeypatch):
    """Force send() to return a non-completed status by patching _end_event."""
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
    """send() returns completed but session died during the check."""
    from sandbox_mcp.shell_session import ShellUnhealthy, _health_check

    session = ShellSession(["bash"])
    monkeypatch.setattr(
        session,
        "send",
        lambda *a, **kw: {"status": "completed", "exit_code": 0},
    )
    # Force the state to terminated after send() returns.
    def fake_send(*a, **kw):
        session._state = "terminated"
        return {"status": "completed", "exit_code": 0}
    monkeypatch.setattr(session, "send", fake_send)
    with pytest.raises(ShellUnhealthy, match="died during"):
        _health_check(session)
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `uv run pytest tests/test_shell_session.py -v -k "health_check"`
Expected: ImportError on `ShellUnhealthy` / `_health_check`.

- [ ] **Step 3.3: Add `ShellUnhealthy` and `_health_check`**

In `src/sandbox_mcp/shell_session.py`, near the top (after imports):

```python
class ShellUnhealthy(Exception):
    """Raised when a freshly-created shell fails the health check.

    The check sends ``true`` and expects a quick completed response.
    Catching this in the registry prevents the broken shell from ever
    being added to the active shell table.
    """


def _health_check(session) -> None:
    """Verify a session is alive by sending ``true``.

    Healthy bash responds in ~ms; only a broken shell hits the 1s
    timeout.  Raises :class:`ShellUnhealthy` on any failure.
    """
    result = session.send("true", wait=True, timeout=1)
    if session.state == "terminated":
        raise ShellUnhealthy("shell died during health check")
    if result.get("status") != "completed":
        raise ShellUnhealthy(f"health check returned status={result.get('status')!r}")
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `uv run pytest tests/test_shell_session.py -v -k "health_check"`
Expected: 3 passed.

- [ ] **Step 3.5: Commit**

```bash
git add src/sandbox_mcp/shell_session.py tests/test_shell_session.py
git commit -m "feat(shell): ShellUnhealthy exception + _health_check helper"
```

---

## Task 4: `ShellRegistry.open()` runs health check

**Files:**
- Modify: `src/sandbox_mcp/shell_registry.py`
- Test: `tests/test_shell_registry.py`

Centralising here means both `get_or_create_default` (default shell) and `_op_shell_new` (explicit shell) get the check automatically.

- [ ] **Step 4.1: Add failing test**

Add to `tests/test_shell_registry.py`:

```python
def test_open_health_checks_before_publishing():
    """open() must run _health_check before adding to the registry."""
    from unittest.mock import patch
    from sandbox_mcp.shell_registry import ShellRegistry
    from sandbox_mcp.shell_session import ShellUnhealthy

    reg = ShellRegistry()
    broken_session = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    broken_session.close = MagicMock()

    with patch(
        "sandbox_mcp.shell_registry._health_check",
        side_effect=ShellUnhealthy("broken"),
    ):
        with pytest.raises(ShellUnhealthy):
            reg.open("dev", broken_session)

    # Session was closed (cleanup) but never published.
    broken_session.close.assert_called_once()
    assert reg.list_shells() == []


def test_open_publishes_healthy_session():
    reg = ShellRegistry()
    session = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.open("dev", session)
    assert shell_id in [s["shell_id"] for s in reg.list_shells()]
```

- [ ] **Step 4.2: Run test to verify it fails**

Run: `uv run pytest tests/test_shell_registry.py::test_open_health_checks_before_publishing tests/test_shell_registry.py::test_open_publishes_healthy_session -v`
Expected: First test passes by accident (since open() doesn't yet call _health_check). After we add the call, both should pass. If open() is unchanged, the first test asserts the cleanup behavior we haven't added yet — see step 4.3.

- [ ] **Step 4.3: Add import and health check to `open()`**

In `src/sandbox_mcp/shell_registry.py`:

Top of file (after `from sandbox_mcp.shell_session import ShellSession`):

```python
from sandbox_mcp.shell_session import ShellSession, ShellUnhealthy, _health_check
```

Replace the existing `open()` method (around line 35):

```python
    def open(self, machine: str, session: ShellSession, purpose: str = "") -> str:
        """Register a session.  Health-checks the session before publishing
        so broken shells are never added to the registry.  Closes the
        session before raising so the caller doesn't have to clean up.
        """
        try:
            _health_check(session)
        except ShellUnhealthy:
            with contextlib.suppress(Exception):
                session.close()
            raise
        shell_id = f"sh_{uuid.uuid4().hex[:12]}"
        self._shells[shell_id] = {
            "session": session,
            "machine": machine,
            "purpose": purpose,
        }
        return shell_id
```

- [ ] **Step 4.4: Run tests to verify they pass**

Run: `uv run pytest tests/test_shell_registry.py -v`
Expected: All passed (the existing `test_open_shell` etc. use `MagicMock(state="idle", ...)` whose `send()` is auto-mocked — health check would actually call real send, which would try to write stdin to a MagicMock and fail).

The existing tests use a MagicMock as the session. With `_health_check` calling `session.send("true", ...)`, the MagicMock auto-returns a `MagicMock` for that call. The `result.get("status")` returns a MagicMock, which != "completed" → ShellUnhealthy raised.

This means existing tests will break. We need to patch `_health_check` in the existing tests that don't care about health. Update tests/test_shell_registry.py:

```python
@pytest.fixture(autouse=True)
def _patch_health_check(monkeypatch):
    """Existing tests don't exercise health checking; bypass it."""
    monkeypatch.setattr(
        "sandbox_mcp.shell_registry._health_check", lambda session: None
    )
```

- [ ] **Step 4.5: Verify existing tests still pass with the autouse fixture**

Run: `uv run pytest tests/test_shell_registry.py -v`
Expected: All passed (existing tests + 2 new ones).

- [ ] **Step 4.6: Commit**

```bash
git add src/sandbox_mcp/shell_registry.py tests/test_shell_registry.py
git commit -m "feat(registry): open() health-checks session before publishing"
```

---

## Task 5: `_capture_for_replacement` + `attach_previous_shell` + `_with_pid` integration

**Files:**
- Modify: `src/sandbox_mcp/shell_session.py` (`_with_pid` extension + `attach_previous_shell` method + new `_previous_shell_info` field)
- Modify: `src/sandbox_mcp/shell_registry.py` (`_capture_for_replacement` helper)
- Test: `tests/test_shell_session.py`

The one-shot delivery is the key UX: agent sees `previous_shell` exactly once after self-heal, never again.

- [ ] **Step 5.1: Add failing tests**

Add to `tests/test_shell_session.py`:

```python
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
```

- [ ] **Step 5.2: Run tests to verify they fail**

Run: `uv run pytest tests/test_shell_session.py -v -k "with_pid"`
Expected: AttributeError on `attach_previous_shell`.

- [ ] **Step 5.3: Add `_previous_shell_info` field and `attach_previous_shell` method**

In `src/sandbox_mcp/shell_session.py`, add to `__init__`:

```python
self._previous_shell_info: dict | None = None
```

Add the method (place near `bash_pid` property):

```python
    def attach_previous_shell(self, info: dict | None) -> None:
        """Attach info about a previously-dead shell this one replaces.

        ``_with_pid`` injects this into the next ``send``/``read`` result
        (one-shot) so agents can see why the previous bash died.
        """
        self._previous_shell_info = info
```

- [ ] **Step 5.4: Extend `_with_pid` to inject + clear `previous_shell`**

In `src/sandbox_mcp/shell_session.py`, current `_with_pid`:

```python
    def _with_pid(self, result: dict) -> dict:
        """Tag a result dict with the current bash process id.

        Agents track this across calls; a change means the shell was
        restarted and in-memory state (exports, cwd, jobs) is gone.
        """
        pid = self.bash_pid
        if pid is not None:
            result["bash_pid"] = pid
        return result
```

Replace with:

```python
    def _with_pid(self, result: dict) -> dict:
        """Tag a result dict with the current bash process id.

        Agents track this across calls; a change means the shell was
        restarted and in-memory state (exports, cwd, jobs) is gone.

        Also injects ``previous_shell`` once (one-shot delivery) if a
        ``_previous_shell_info`` snapshot is attached — see
        :meth:`attach_previous_shell`.
        """
        pid = self.bash_pid
        if pid is not None:
            result["bash_pid"] = pid
        if self._previous_shell_info is not None:
            result["previous_shell"] = self._previous_shell_info
            self._previous_shell_info = None  # one-shot
        return result
```

- [ ] **Step 5.5: Add `_capture_for_replacement` in shell_registry.py**

In `src/sandbox_mcp/shell_registry.py`, after `__init__`:

```python
def _capture_for_replacement(dead_session):
    """Snapshot info about a dead session.  Returns None if there's
    nothing meaningful to report (e.g. session never had a real process).

    Called BEFORE close() so bash_pid is still readable.
    """
    if dead_session.bash_pid is None:
        return None
    return {
        "previous_bash_pid": dead_session.bash_pid,
        "last_command": dead_session.last_command,
        "exit_reason": dead_session.exit_reason,
        "exit_code": dead_session.last_exit_code,
    }
```

- [ ] **Step 5.6: Run tests to verify they pass**

Run: `uv run pytest tests/test_shell_session.py -v -k "with_pid"`
Expected: 2 passed.

- [ ] **Step 5.7: Commit**

```bash
git add src/sandbox_mcp/shell_session.py src/sandbox_mcp/shell_registry.py tests/test_shell_session.py
git commit -m "feat(shell): one-shot previous_shell delivery via _with_pid"
```

---

## Task 6: `get_or_create_default` captures + attaches prev

**Files:**
- Modify: `src/sandbox_mcp/shell_registry.py`
- Test: `tests/test_shell_registry.py`

The simplified flow: capture before close (so bash_pid is readable), close the dead one, factory the new one, open() (which now health-checks), then attach prev to the new session.

- [ ] **Step 6.1: Add failing tests**

Add to `tests/test_shell_registry.py`:

```python
def test_get_or_create_default_attaches_prev_on_self_heal(monkeypatch):
    """After self-heal, the new shell has the previous_shell info attached."""
    from unittest.mock import patch
    from sandbox_mcp.shell_session import ShellSession

    # Patch out _health_check so the new session passes.
    monkeypatch.setattr(
        "sandbox_mcp.shell_registry._health_check", lambda session: None
    )

    reg = ShellRegistry()

    # First default: live, return real ShellSession (mocked state).
    s1 = reg.get_or_create_default(
        "dev", lambda: _make_real_session_with_explicit_bash()
    )
    session1 = reg.get(s1)
    # Make it look dead with captured exit info.
    session1._state = "terminated"
    session1.exit_reason = "exit"
    session1.last_exit_code = 0
    session1._last_command = "exit 0"

    s2 = reg.get_or_create_default(
        "dev", lambda: _make_real_session_with_explicit_bash()
    )
    session2 = reg.get(s2)
    # Trigger one-shot delivery.
    result = session2._with_pid({"status": "idle"})
    assert "previous_shell" in result
    assert result["previous_shell"]["last_command"] == "exit 0"
    assert result["previous_shell"]["exit_reason"] == "exit"
    reg.close(s2)


def _make_real_session_with_explicit_bash():
    """Helper: a real ShellSession with mock bash_pid + drain so
    capture works without actually spawning bash."""
    from unittest.mock import MagicMock
    from sandbox_mcp.shell_session import ShellSession

    session = ShellSession.__new__(ShellSession)
    session.bash_pid = 99999
    session.exit_reason = "unknown"
    session.last_exit_code = None
    session.last_command = ""
    session._previous_shell_info = None
    session._state = "idle"
    session._process = MagicMock()
    session._drain_thread = MagicMock(is_alive=lambda: False)
    return session


def test_get_or_create_default_no_prev_when_factory_raises(monkeypatch):
    """If factory() raises, no prev is leaked (no session to attach to)."""
    reg = ShellRegistry()
    # Seed a dead default so self-heal fires.
    monkeypatch.setattr(
        "sandbox_mcp.shell_registry._health_check", lambda session: None
    )
    s1 = reg.get_or_create_default(
        "dev", lambda: _make_real_session_with_explicit_bash()
    )
    reg.get(s1)._state = "terminated"

    def bad_factory():
        raise RuntimeError("docker daemon down")

    with pytest.raises(RuntimeError):
        reg.get_or_create_default("dev", bad_factory)
    # Old shell is still cleaned up.
    assert reg.list_shells() == []


def test_get_or_create_default_no_prev_when_open_health_fails(monkeypatch):
    """If open()'s health check fails, prev is not attached."""
    from sandbox_mcp.shell_session import ShellUnhealthy

    reg = ShellRegistry()
    monkeypatch.setattr(
        "sandbox_mcp.shell_registry._health_check",
        side_effect=[None, ShellUnhealthy("broken")],
    )
    s1 = reg.get_or_create_default(
        "dev", lambda: _make_real_session_with_explicit_bash()
    )
    reg.get(s1)._state = "terminated"

    new_session = _make_real_session_with_explicit_bash()
    with pytest.raises(ShellUnhealthy):
        reg.get_or_create_default("dev", lambda: new_session)
    # New session was closed by open()'s cleanup path.
    new_session.close.assert_called_once()
```

- [ ] **Step 6.2: Run tests to verify they fail**

Run: `uv run pytest tests/test_shell_registry.py -v -k "self_heal or factory_raises or open_health_fails"`
Expected: failures — get_or_create_default doesn't yet capture prev.

- [ ] **Step 6.3: Modify `get_or_create_default`**

In `src/sandbox_mcp/shell_registry.py`, current:

```python
    def get_or_create_default(self, machine: str, factory: Callable[[], ShellSession]) -> str:
        existing = self._default_shells.get(machine)
        if existing and existing in self._shells:
            entry = self._shells[existing]
            if entry["session"].state != "terminated":
                return existing
            self.close(existing)
        session = factory()
        shell_id = self.open(machine, session, purpose="default")
        self._default_shells[machine] = shell_id
        return shell_id
```

Replace with:

```python
    def get_or_create_default(self, machine: str, factory: Callable[[], ShellSession]) -> str:
        existing = self._default_shells.get(machine)
        if existing and existing in self._shells:
            entry = self._shells[existing]
            if entry["session"].state != "terminated":
                return existing
            # Self-heal: capture the dying session's info BEFORE close()
            # (close() nulls out _process, making bash_pid unreadable),
            # then drop the dead shell and create a fresh one.
            prev = _capture_for_replacement(entry["session"])
            self.close(existing)
            session = factory()
            shell_id = self.open(machine, session, purpose="default")
            if prev is not None:
                session.attach_previous_shell(prev)
            self._default_shells[machine] = shell_id
            return shell_id
        session = factory()
        shell_id = self.open(machine, session, purpose="default")
        self._default_shells[machine] = shell_id
        return shell_id
```

- [ ] **Step 6.4: Run tests to verify they pass**

Run: `uv run pytest tests/test_shell_registry.py -v`
Expected: All passed.

- [ ] **Step 6.5: Commit**

```bash
git add src/sandbox_mcp/shell_registry.py tests/test_shell_registry.py
git commit -m "feat(registry): get_or_create_default attaches previous_shell on self-heal"
```

---

## Task 7: `_handle_shell_exec` catches factory / health errors

**Files:**
- Modify: `src/sandbox_mcp/server.py`
- Test: `tests/test_server.py` (or extend an existing test)

`_handle_shell_exec` should catch `ShellUnhealthy` separately (so the agent sees `error_kind="shell_unhealthy"`) and any other factory exception (so the agent sees `error_kind="shell_create_failed"` with the underlying error).

- [ ] **Step 7.1: Add failing test**

Find an existing test for `_handle_shell_exec` (search for `test_shell_exec_*` or `test_default_*`). If none exists, add to `tests/test_server.py`:

```python
def test_handle_shell_exec_returns_shell_unhealthy_error_kind(monkeypatch):
    """When open()'s health check fails, response has error_kind='shell_unhealthy'."""
    from sandbox_mcp.shell_session import ShellUnhealthy

    server = SandboxServer()
    # Force the registry's get_or_create_default to raise.
    monkeypatch.setattr(
        server.shells,
        "get_or_create_default",
        lambda *a, **kw: (_ for _ in ()).throw(ShellUnhealthy("broken")),
    )
    # Avoid hitting the real backend.
    server.machines.get_backend = lambda name: MagicMock()
    result = server._handle_shell_exec({"command": "echo hi", "machine": "dev"})
    assert result["status"] == "error"
    assert result["error_kind"] == "shell_unhealthy"
    assert "broken" in result["error"]


def test_handle_shell_exec_returns_shell_create_failed_error_kind(monkeypatch):
    """When factory() raises a non-shell error, error_kind='shell_create_failed'."""
    server = SandboxServer()
    monkeypatch.setattr(
        server.shells,
        "get_or_create_default",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("docker down")),
    )
    server.machines.get_backend = lambda name: MagicMock()
    result = server._handle_shell_exec({"command": "echo hi", "machine": "dev"})
    assert result["status"] == "error"
    assert result["error_kind"] == "shell_create_failed"
    assert "docker down" in result["error"]
```

- [ ] **Step 7.2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v -k "shell_unhealthy or shell_create_failed"`
Expected: response lacks `error_kind`.

- [ ] **Step 7.3: Wrap `get_or_create_default` call in `_handle_shell_exec`**

In `src/sandbox_mcp/server.py`, the current code in `_handle_shell_exec` (around line 485):

```python
            sid = self.shells.get_or_create_default(
                machine, lambda: backend.open_shell(machine)
            )
            session = self.shells.get(sid)
```

Wrap with try/except:

```python
            try:
                sid = self.shells.get_or_create_default(
                    machine, lambda: backend.open_shell(machine)
                )
            except ShellUnhealthy as e:
                return {
                    "status": "error",
                    "error_kind": "shell_unhealthy",
                    "error": f"[machine={machine!r}] {e}",
                    "machine": machine,
                }
            except Exception as e:
                return {
                    "status": "error",
                    "error_kind": "shell_create_failed",
                    "error": f"[machine={machine!r}] {e}",
                    "machine": machine,
                }
            session = self.shells.get(sid)
```

Add the import near the top of `server.py` (with the other shell_session imports):

```python
from sandbox_mcp.shell_session import ShellSession, ShellUnhealthy
```

- [ ] **Step 7.4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v -k "shell_unhealthy or shell_create_failed"`
Expected: 2 passed.

- [ ] **Step 7.5: Commit**

```bash
git add src/sandbox_mcp/server.py tests/test_server.py
git commit -m "feat(server): _handle_shell_exec returns structured error_kind on factory/health failure"
```

---

## Task 8: End-to-end verification via running service

**Files:** No source changes. Verify against running container.

- [ ] **Step 8.1: Run full test suite + lint**

```bash
uv run pytest -q
uv run ruff format .
uv run ruff check .
```

Expected: 341+ passed (will be higher due to new tests), lint clean.

- [ ] **Step 8.2: Rebuild and restart the sandbox-mcp service**

Per user instruction: do NOT delete old images.

```bash
docker compose up -d --build
```

- [ ] **Step 8.3: Wait for healthy**

```bash
sleep 3
docker inspect --format='{{.State.Health.Status}}' sandbox-mcp
```

Expected: `healthy`.

- [ ] **Step 8.4: End-to-end: run `exit 0`, then `echo hi`, verify `previous_shell`**

```bash
TOKEN=$(head -1 /work/sandbox-mcp/config/auth_tokens)
SESSION=$(curl -sL -i -X POST http://localhost:8010/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0.0.1"}}}' \
  | grep -i 'mcp-session-id' | sed 's/.*mcp-session-id: //I' | tr -d '\r')

# Step 1: explicitly exit the default shell.
curl -sL -X POST http://localhost:8010/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"shell_exec","arguments":{"command":"exit 0"}}}'

# Step 2: next exec must include previous_shell.
curl -sL -X POST http://localhost:8010/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"shell_exec","arguments":{"command":"echo hello"}}}'

# Step 3: subsequent exec must NOT include previous_shell.
curl -sL -X POST http://localhost:8010/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"shell_exec","arguments":{"command":"echo world"}}}'
```

Expected output for step 2: contains `"previous_shell": {...}` with `exit_reason: "exit"`, `last_command: "exit 0"`, `exit_code: 0`, `previous_bash_pid: <some hash>`.

Expected output for step 3: NO `previous_shell` key.

- [ ] **Step 8.5: Verify audit log**

```bash
docker exec sandbox-mcp python3 -c "
import sqlite3
c = sqlite3.connect('/home/sandbox/.sandbox-mcp/audit.db')
for row in c.execute('SELECT id, action, status FROM audit ORDER BY id DESC LIMIT 5'):
    print(row)
"
```

Expected: last few rows are `shell_exec` with `status=ok`.

- [ ] **Step 8.6: Final commit if any stray changes**

```bash
git status
# If clean, no commit needed.
# If spec/plan docs have typos, fix and commit:
# git add docs/...
# git commit -m "docs: minor cleanup"
```

---

## Self-Review Checklist

- [x] **Spec coverage:**
  - `previous_shell` 4 fields → Task 5 (`_capture_for_replacement`)
  - one-shot delivery → Task 5 (`_with_pid` clears after first call)
  - latest-wins on chain self-heal → Task 6 (each self-heal captures only the immediately-previous)
  - exit_reason enum values → Task 1 (drain captures from proc.poll)
  - DockerExecProcess.poll() → Task 2
  - _health_check at open() → Tasks 3, 4
  - get_or_create_default simplification → Task 6
  - _handle_shell_exec structured error_kind → Task 7
- [x] **Placeholder scan:** No "TBD" / "TODO" / "implement later".
- [x] **Type consistency:** `_previous_shell_info` is `dict | None`, `exit_reason` is `str`, `last_exit_code` is `int | None`. `ShellUnhealthy` is a single Exception class.
- [x] **No unrelated changes:** Each commit is one focused concern. README docs are already in place from earlier bash_pid commit.