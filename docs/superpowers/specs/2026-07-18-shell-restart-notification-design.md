# Shell Restart Notification — Design Spec

Date: 2026-07-18
Status: Draft (pending user review)

## Problem

`sandbox-mcp` keeps a persistent bash subprocess per `shell_exec`
session so the agent's `$FOO=bar`, `cd /workspace`, background jobs,
etc. carry across calls. Today, if that bash dies — whether the agent
runs `exit`, the bash process gets OOM-killed, or anything else — the
next `shell_exec` transparently runs in a fresh bash and the agent has
no way to know:

- The shell was restarted.
- *Why* the previous shell died (`exit N`, signal, broken pipe).
- *What command* the agent ran on the now-dead shell.

Commit `1007168` introduced silent self-heal (drop dead default shell,
create fresh one). Commit `6bca12b` introduced a `bash_pid` field on
`send`/`read` results so agents can detect a restart *after the fact*
by comparing PIDs across calls.

What `bash_pid` alone can't do:

- Tell the agent *what happened* on the dead shell.
- Distinguish "I ran `exit 0` myself" (no action needed) from "the
  shell died unexpectedly" (state is gone, agent must re-establish).
- Be self-explanatory — the agent has to maintain its own PID log and
  diff against it on every call.

## Goal

Add a `previous_shell` field to `send`/`read` results that, when
present, summarizes the death of the *previous* bash instance. The
field is delivered **at most once** per replacement shell — agents
that never look at it still benefit from later deliveries on
subsequent replacements (latest-wins, not chain-merged).

Field schema:

```json
"previous_shell": {
  "previous_bash_pid": 12345,
  "last_command": "bash my-build.sh",
  "exit_reason": "exit",       // "exit" | "signal" | "broken_pipe" | "unknown"
  "exit_code": 42               // int | null
}
```

- `previous_bash_pid`: process ID of the dead shell. Matches the
  `bash_pid` the agent saw on its previous `shell_exec` response, so
  the agent can correlate without bookkeeping.
- `last_command`: the last command the agent sent to the dead shell
  (`ShellSession.last_command`). Lets the agent spot "oh, my `rm -rf`
  probably killed the shell".
- `exit_reason`: high-level category of death:
  - `"exit"` — bash ran `exit N` (or `bash` itself returned to its
    caller normally).
  - `"signal"` — bash was killed by a signal (e.g. SIGKILL from our
    own `close()`, or external OOM killer).
  - `"broken_pipe"` — we tried to write the next command's markers to
    bash's stdin and the pipe was already closed (shell died between
    calls or before our first write).
  - `"unknown"` — couldn't determine (defensive fallback).
- `exit_code`: numeric value when meaningful (`exit` → N, `signal` →
  signal number), `null` otherwise.

## Non-Goals

- Chain-merged history (only the immediately-previous shell is
  reported; deeper history is overwritten).
- Persisted across server restart (in-memory only, like the rest of
  the shell registry).
- Reporting previous_shell for explicit agent-initiated close
  (`shell_remove`, `close_all_for_machine`) — agent already knows.
- Reporting previous_shell for `shell_new` — there's no "previous"
  shell to report against.
- Limiting self-heal chain (e.g. rate-limiting repeated self-heal
  attempts). Defer until we see it actually cycle in production.

## Design

### Where the data is captured

`ShellSession` gains two new fields, populated on death:

| Field | Type | Set when |
|---|---|---|
| `exit_reason` | `"exit"` \| `"signal"` \| `"broken_pipe"` \| `"unknown"` | drain thread sees EOF (bash closed stdout); or send() raises BrokenPipeError; or close() runs killpg |
| `last_exit_code` | `int \| None` | same points |

For local `subprocess.Popen`, `proc.returncode` follows the convention:
`N` for `exit N`, `-N` for killed by signal N, `None` while running.
So `returncode > 0` → `("exit", N)`, `returncode == 0` →
`("exit", 0)`, `returncode < 0` → `("signal", -returncode)`,
`BrokenPipeError` → `("broken_pipe", None)`.

For `DockerExecProcess` (the docker backend's bash), we add a
`poll()` method that calls `exec_inspect(exec_id)` and returns
`{"exit_code": int | None}`. Same mapping applies.

### Where the data flows

`ShellRegistry.open()` becomes the single entry point that both
health-checks *and* registers a session. Today:

```python
def open(self, machine, session, purpose=""):
    shell_id = f"sh_{uuid.uuid4().hex[:12]}"
    self._shells[shell_id] = {...}
    return shell_id
```

After this change:

```python
def open(self, machine, session, purpose=""):
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
    self._shells[shell_id] = {...}
    return shell_id
```

Two callers benefit automatically:

1. `get_or_create_default` — default shell path used by `shell_exec`.
2. `_op_shell_new` in `sandbox_env.py` — explicit `shell_new` action.

`get_or_create_default` becomes simpler because the health-check
moves out:

```python
def get_or_create_default(self, machine, factory):
    existing = self._default_shells.get(machine)
    if existing and existing in self._shells:
        entry = self._shells[existing]
        if entry["session"].state != "terminated":
            return existing
        # Self-heal path
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

`_capture_for_replacement` reads from the dead session *before*
`close()`, because `close()` nulls out `_process` and `bash_pid`
becomes `None`:

```python
def _capture_for_replacement(dead_session):
    if dead_session.bash_pid is None:
        return None  # never had a real process — nothing meaningful
    return {
        "previous_bash_pid": dead_session.bash_pid,
        "last_command": dead_session.last_command,
        "exit_reason": dead_session.exit_reason,
        "exit_code": dead_session.last_exit_code,
    }
```

### One-shot delivery

`ShellSession` holds the snapshot until the next `send` or `read`
consumes it. Today `_with_pid` already injects `bash_pid`; we extend
it:

```python
def _with_pid(self, result):
    if self.bash_pid is not None:
        result["bash_pid"] = self.bash_pid
    if self._previous_shell_info is not None:
        result["previous_shell"] = self._previous_shell_info
        self._previous_shell_info = None  # one-shot
    return result
```

Latest-wins: if the agent never consumes the snapshot and the
session itself dies and gets replaced, the next shell's
`_previous_shell_info` is the *current* session's death info —
overwriting the older one. The chain-merging alternative
(`previous_shell.inherited_from = {...}`) was considered and
rejected: nested records make the agent's parser more complex and
the older info is rarely still actionable.

### `_health_check`

```python
def _health_check(session):
    """Verify the freshly-created session is actually alive.

    Healthy bash responds to ``true`` in ~ms; only a broken shell
    hits the 1s timeout.  Raises ``ShellUnhealthy`` if:
      - status != "completed" (timeout / broken pipe / idle)
      - session.state == "terminated" (died during check)
    """
    result = session.send("true", wait=True, timeout=1)
    if session.state == "terminated":
        raise ShellUnhealthy("shell died during health check")
    if result["status"] != "completed":
        raise ShellUnhealthy(f"health check returned status={result['status']!r}")
```

The `true` noop is a bash builtin, so healthy check overhead is
~10–30ms (marker write + echo back). Only a broken shell waits the
full 1s.

### Error surfacing

`SandboxServer._handle_shell_exec` wraps `get_or_create_default`
with two distinct catches so the agent gets actionable error_kind:

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
```

`_op_shell_new` is unchanged — `SandboxEnv.dispatch()`
(`sandbox_env.py:531–535`) already catches generic exceptions and
returns `{"error": ..., "type": "ShellUnhealthy"}`, which is
descriptive enough for the explicit-create case.

## Test Plan

### Unit (`tests/test_shell_session.py`)

- `test_health_check_passes_for_fresh_session`
- `test_health_check_raises_when_send_returns_terminated`
- `test_health_check_raises_when_session_state_terminated`
- `test_drain_captures_exit_reason_exit`
- `test_drain_captures_exit_reason_signal` (kill -9 the bash subprocess)
- `test_send_captures_broken_pipe_exit_reason`
- `test_bash_pid_is_none_after_close` (precondition for capture ordering)

### Unit (`tests/test_shell_registry.py`)

- `test_open_health_checks_before_publishing`: broken session →
  ShellUnhealthy, not in registry, session.close called
- `test_get_or_create_default_attaches_prev_on_self_heal`
- `test_get_or_create_default_no_prev_when_factory_raises`
- `test_get_or_create_default_no_prev_when_open_health_fails`
- `test_get_or_create_default_replaces_dead_shell` (existing, still passes)

### Integration (existing harness)

- `test_exit_then_exec_returns_previous_shell`: local bash, run
  `exit 0`, next `send` returns `previous_shell` with
  `exit_reason="exit"`.
- `test_previous_shell_one_shot`: second consecutive `send` has no
  `previous_shell`.
- `test_no_previous_shell_on_explicit_shell_remove`: after
  `shell_remove` on the default, next `send` has no `previous_shell`.
- `test_no_previous_shell_on_shell_new`: explicit `shell_new`
  response has no `previous_shell`.

### Docker backend (manual / integration marker)

- `tests/test_docker_backend.py::test_docker_exec_process_poll`
  — verify the new `poll()` method returns proper exit code / None.

## Open Questions

- None.  All decisions confirmed during brainstorming on 2026-07-18.

## Changelog

- 2026-07-18: Initial draft.