# Docker Introspection Tools — Design Spec

Date: 2026-07-16
Status: Approved

## Problem

sandbox-mcp currently exposes eight `env`-tool docker actions:
`docker_run`, `docker_build`, `docker_commit`, `docker_stop`,
`docker_start`, `docker_remove`, `docker_ps`, `docker_images`. None
of them let the agent *introspect* a container or *observe* its
runtime state.

Today, when an agent needs to debug a container, it has only
`shell_exec` plus the curated fields returned by `docker_ps`
(name/status/image/purpose/created). It cannot:

- Inspect container config (`Cmd`, `Entrypoint`, mounts, labels,
  restart policy) — needed when a container behaves unexpectedly
  ("why did my build image run with `bash`, not `/bin/sh`?")
- Read container logs — needed when a `docker_run` returns an
  `error` TargetInfo (the truncated `tail=20` hint in
  `_running_info` is already the workaround, but only at create time)
- See filesystem drift vs the image — needed before `docker_commit`
  to confirm "this is the layer of changes I want to save"
- Observe live resource usage — needed when diagnosing OOM / CPU
  saturation / runaway processes
- Atomically restart a container — today the agent must call
  `docker_stop` then `docker_start`, with a window where the machine
  is reported as `stopped`

`shell_exec` covers some of these (`env`, `pwd`, `whoami`, `hostname
-i`), but not the things that are genuinely docker-level config or
runtime state.

## Goal

Add five read-only-or-idempotent docker introspection actions to the
existing `env` tool, matching the existing dispatch pattern. Total
top-level MCP tool count remains 7.

The five actions:

| action            | type   | purpose                                          |
|-------------------|--------|--------------------------------------------------|
| `docker_inspect`  | read   | Curated container config + raw escape hatch      |
| `docker_logs`     | read   | Tail/since/until-filtered container logs         |
| `docker_diff`     | read   | Filesystem changes vs image (A / C / D)          |
| `docker_stats`    | read   | One-shot CPU / memory / network / block IO       |
| `docker_restart`  | write  | Atomic `stop` + `start`                         |

## Non-Goals

- **No streaming `stats` / `logs`** — the MCP tool-call model is
  request/response; a long-lived stream would either block the
  agent or require a separate subscription mechanism. Agents needing
  live monitoring call `docker_stats` repeatedly.
- **No new top-level MCP tools** — these ship as `env` tool actions,
  consistent with all existing docker lifecycle operations.
- **No SSH backend equivalents** — only the docker backend gains
  these. SSH machines don't have an "image" or "filesystem diff"
  concept that maps cleanly. Add SSH variants later if a concrete
  use case surfaces.
- **No write to the audit log from inside container logs** — the
  existing `content_sha256` audit handling applies to *file write*
  payloads, not to *container logs*, and is out of scope here.
- **No change to `docker_ps` / `docker_images` output** — these
  existing actions are kept as-is.

## Design

### Layering

All new logic lives in `DockerBackend`; `SandboxEnv` dispatchers
are thin wrappers. This matches the existing pattern of
`list_managed_containers` / `list_images` (curated dicts returned
from backend methods) and keeps the SDK boundary in one class.

```
agent → env action=… → SandboxEnv._op_docker_X → DockerBackend.X → docker SDK
```

### Action 1: `docker_inspect`

Curated view by default; `raw=true` returns the full `container.attrs`
dict.

**Curated fields** (default):

```json
{
  "id": "abc123def456",
  "name": "dev",
  "image": "python:3.12-slim",
  "created": "2026-07-16T10:00:00Z",
  "started_at": "2026-07-16T10:00:01Z",
  "finished_at": "0001-01-01T00:00:00Z",
  "state": {
    "status": "running",
    "running": true,
    "exit_code": 0,
    "error": "",
    "restart_count": 0,
    "health": "healthy"
  },
  "cmd": ["python", "-m", "http.server"],
  "entrypoint": null,
  "mounts": [
    {"source": "/home/x/.sandbox-mcp/workspaces/dev",
     "destination": "/workspace", "mode": "rw"},
    {"source": "/home/x/.sandbox-mcp/workspaces/_share",
     "destination": "/workspace/share", "mode": "ro"}
  ],
  "labels": {
    "sandbox-mcp.managed": "true",
    "sandbox-mcp.machine": "dev",
    "sandbox-mcp.purpose": "Python dev"
  },
  "restart_policy": {"name": "unless-stopped", "max_retry": 0}
}
```

**Deliberately omitted** (because `shell_exec` can answer them):

- `Config.Env` → `printenv`
- `Config.WorkingDir` → `pwd`
- `Config.User` → `whoami`
- `NetworkSettings.IPAddress` → `hostname -i`

**Parameters:**

| name   | type | default | notes                                  |
|--------|------|---------|----------------------------------------|
| `machine` | str  | —       | required, resolved via registry     |
| `raw`  | bool | false   | when true, return full `attrs` dict    |

**Behaviour:**

- `container.reload()` before reading attrs (fresh state)
- `raw=true` returns `attrs` as-is (caller can read every key,
  including `Config.Env` values, `NetworkSettings`, `HostConfig`)
- Errors: container not found, daemon API error → `{"error": str(e),
  "status": "error"}` (consistent with `commit` / `build`)

### Action 2: `docker_logs`

Tail/since/until/timestamps-filtered container log readback.

**Parameters:**

| name          | type | default  | notes                                         |
|---------------|------|----------|-----------------------------------------------|
| `machine`     | str  | —        | required                                      |
| `tail`        | int  | 200      | max 10000 (hard cap; `ValueError` otherwise)  |
| `since`       | str  | None     | ISO 8601 timestamp (`2026-07-16T10:00:00Z`) or relative (`"10m"`) |
| `until`       | str  | None     | same format                                   |
| `timestamps`  | bool | false    | prefix each line with RFC 3339 timestamp      |

**Return shape:**

```json
{"logs": "2026-07-16 10:00:01 INFO starting server\n...",
 "truncated": false}
```

`truncated=true` when output is clipped by `tail` or by the 10000
hard cap.

**Behaviour:**

- `container.logs(tail=..., since=..., until=..., timestamps=...)`
- stdout and stderr merged (matches `exec_oneoff` `demux=False`)
- Bytes decoded as utf-8 with `errors="replace"`
- Works against stopped containers (Docker keeps the log buffer;
  this is the primary use case for the action — reading why a
  container exited)

### Action 3: `docker_diff`

Filesystem changes vs the image the container was started from.

**Parameters:**

| name      | type | default | notes     |
|-----------|------|---------|-----------|
| `machine` | str  | —       | required  |

**Return shape:**

```json
{"changes": {"A": ["/workspace/new_file.txt"],
              "C": ["/workspace/config.yaml"],
              "D": ["/workspace/old.log"]},
 "summary": {"added": 1, "changed": 1, "deleted": 1}}
```

`A`/`C`/`D` map to docker SDK's `Kind` integer (`0=Modified → C`,
`1=Added → A`, `2=Deleted → D`).

**Behaviour:**

- `container.diff()` returns `[{"Path": str, "Kind": int}, ...]`
- Group by kind; sort each group alphabetically
- No `raw` escape hatch — output is already compact and structured

### Action 4: `docker_stats`

One-shot resource-usage snapshot.

**Parameters:**

| name      | type | default | notes                                |
|-----------|------|---------|--------------------------------------|
| `machine` | str  | —       | required                             |
| `stream`  | bool | false   | **rejected**: error if true          |

**Return shape (curated):**

```json
{
  "cpu_percent": 12.3,
  "memory": {"usage_bytes": 52428800, "limit_bytes": 1073741824,
             "usage_percent": 4.88},
  "network": {"rx_bytes": 1234567, "tx_bytes": 987654},
  "block_io": {"read_bytes": 4096, "write_bytes": 8192}
}
```

**Behaviour:**

- `container.stats(stream=False)` returns a single stats dict
- Compute `cpu_percent` from
  `cpu_stats["cpu_usage"]["total_usage"]` /
  `cpu_stats["system_cpu_usage"]` (precomputed delta is not
  available in a single shot; approximate)
- `network` aggregated across all interfaces (`eth0`, etc.)
- `stream=true` returns `{"error": "streaming is not supported;
  call docker_stats again for the next snapshot", "status":
  "error"}`
- No `raw` escape hatch

### Action 5: `docker_restart`

Atomic `stop` + `start`.

**Parameters:**

| name      | type | default | notes                       |
|-----------|------|---------|-----------------------------|
| `machine` | str  | —       | required                    |
| `timeout` | int  | 10      | seconds for `stop()` phase  |

**Return shape:**

```json
{"machine": "dev", "status": "running"}
```

Or on failure:

```json
{"machine": "dev", "status": "error", "error": "..."}
```

**Behaviour:**

- `container.restart(timeout=timeout)` (single SDK call; Docker
  handles the stop-then-start sequence internally with the right
  sequencing)
- Reuse the existing `_running_info(...)` post-check to verify the
  container is actually running (a crashing `CMD` would otherwise
  leave the agent thinking it's back up)
- For a stopped container this is equivalent to `start` (acceptable;
  saves the agent a state check)

### Backend implementation (`src/sandbox_mcp/backends/docker_backend.py`)

Five new methods on `DockerBackend`:

```python
def inspect(self, name: str, raw: bool = False) -> dict: ...
def logs(self, name: str, *, tail: int = 200, since=None,
         until=None, timestamps: bool = False) -> dict: ...
def diff(self, name: str) -> dict: ...
def stats(self, name: str, *, stream: bool = False) -> dict: ...
def restart(self, name: str, timeout: int = 10) -> TargetInfo: ...
```

All five follow the existing pattern: `containers.get(name)` →
capture `docker.errors.NotFound` and `docker.errors.APIError` →
return `dict` with `error`/`status` keys on failure (or
`TargetInfo` for `restart` to match the lifecycle methods).

Curated inspect output is built from `attrs` keys via direct
dict-access; no JSON path libraries (keeps it readable + grep-able).

### Dispatcher implementation (`src/sandbox_mcp/sandbox_env.py`)

Five new `_op_docker_X` methods following the same pattern as the
existing lifecycle dispatchers (`_op_docker_stop`, `_op_docker_start`,
etc.):

```python
def _op_docker_inspect(self, params):
    err = self._require(params, "machine")
    if err is not None:
        return {"error": err}
    machine = self._machines.resolve_machine(params["machine"])
    backend = self._machines.get_backend(machine)
    if not isinstance(backend, DockerBackend):
        return {"error": "docker_inspect only supported on Docker machines"}
    return backend.inspect(machine, raw=bool(params.get("raw", False)))
```

…plus the same shape for `docker_logs`, `docker_diff`,
`docker_stats`, `docker_restart`. Each dispatcher is < 10 lines.

`DOCKER_HELP_RESPONSE` gains five entries, one per new action, each
with `action` / `description` / `required` / `optional` / `returns`
matching the existing entries' style.

### Security

- All five actions are scoped to the docker backend; an attempt to
  invoke against an SSH machine returns `{"error": "docker_X only
  supported on Docker machines", ...}`
- Machine name is resolved via `TargetRegistry.resolve_machine` —
  agents can only inspect machines they already know about, not
  arbitrary containers on the host
- Read-only ops (`inspect`, `logs`, `diff`, `stats`) carry no
  production impact — same rationale as `docker_images` ("an
  over-broad list leaks information but cannot affect production")
- `docker_inspect` default view deliberately omits `Config.Env`
  values (the agent can read them via `shell_exec env` if needed);
  `raw=true` returns the full attrs including secrets — operators
  who don't trust the agent with that surface can lock it down via
  config (see Open Question 1 below)
- `docker_logs tail` capped at 10000 to prevent token-bombing a
  single response

## Test Plan

### Unit tests (`tests/test_docker_backend.py`)

Reuse the existing mock-container fixtures. New cases per method:

- `inspect()` default returns curated dict with the expected fields
- `inspect(raw=True)` returns full attrs (no curation)
- `inspect()` on a container that disappeared → `{"error": ...,
  "status": "error"}`
- `logs()` defaults to `tail=200`
- `logs(tail=N)` passes through
- `logs()` honors `since` / `until` / `timestamps`
- `logs()` returns `truncated=True` when tail-clipped
- `logs()` byte-decode uses utf-8 with replacement (no exception on
  garbage bytes)
- `logs()` works on stopped containers (no error)
- `diff()` groups by A/C/D correctly
- `diff()` sorts each group alphabetically
- `diff()` empty diff → `{"A": [], "C": [], "D": [],
  "summary": {...}}`
- `stats()` computes `cpu_percent` from a known `cpu_stats` /
  `system_cpu_usage` pair
- `stats()` aggregates `network` across multiple interfaces
- `stats(stream=True)` returns error (refuses streaming)
- `restart(timeout=10)` calls `container.restart(timeout=10)` once
- `restart()` runs `_running_info` post-check; reports "running"
  only when the container actually stayed up

### SandboxEnv dispatch tests (`tests/test_sandbox_env.py` if exists,
else new `tests/test_env_docker.py`)

- `env action=docker_inspect machine=dev raw=true` →
  `backend.inspect("dev", raw=True)` called once
- `env action=docker_logs machine=dev tail=50 timestamps=true` →
  `backend.logs("dev", tail=50, since=None, until=None,
  timestamps=True)` called once
- `env action=docker_inspect machine=remote` (an SSH machine)
  → `{"error": "docker_inspect only supported on Docker machines",
  ...}`
- `env action=docker_help` returns DOCKER_HELP_RESPONSE containing
  all five new action names

### Integration tests (`tests/` `-m integration`)

Skip-by-default; enabled when a docker daemon is reachable.

- Spin up a real container, run a command that exits non-zero,
  `docker_logs machine=X tail=50` returns the exit reason
- Spin up a real container, `docker_diff machine=X` shows files
  written into /workspace
- Spin up a real container, `docker_stats machine=X` returns
  non-zero memory usage

## Migration / Compatibility

- Purely additive at the API level: five new `env` actions, no
  change to existing actions or top-level tools
- No config changes
- No schema changes; no DB migration
- No version bump needed (pre-1.0; semver not yet enforced)

## Open Questions

1. **`raw=true` for inspect** — should operators be able to disable
   the raw escape hatch via config (e.g. `[docker]
   allow_raw_inspect = false`)? Pro: defense in depth against a
   compromised agent exfiltrating secrets via inspect. Con: yet
   another config knob; the agent already has `shell_exec env`. For
   this PR: **ship raw=true unconditionally; defer the config knob
   until someone asks for it.** Reversible later — adding a knob is
   non-breaking; locking down existing behaviour would be.
2. **`since` / `until` relative time format** — Docker SDK accepts
   RFC 3339 timestamps and Go duration strings (`"10m"`, `"1h"`).
   Both are well-defined; no ambiguity. No action needed.