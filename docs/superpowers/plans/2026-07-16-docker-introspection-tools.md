# Docker Introspection Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five read-only-or-idempotent docker introspection actions (`docker_inspect`, `docker_logs`, `docker_diff`, `docker_stats`, `docker_restart`) to the `env` tool.

**Architecture:** New methods on `DockerBackend` (curated dicts, matches `list_images` / `list_managed_containers` pattern); thin dispatchers on `SandboxEnv`; help text updates in `DOCKER_HELP_RESPONSE`. All five actions are accessed via the existing `env` MCP tool (no top-level MCP surface change).

**Tech Stack:** Python 3.x, `docker` Python SDK, existing pytest fixtures (`mock_client`, `docker_backend`, `sandbox_env`).

**Spec:** `docs/superpowers/specs/2026-07-16-docker-introspection-tools-design.md`

---

## File Structure

**Modify:**
- `src/sandbox_mcp/backends/docker_backend.py` — add `inspect`, `logs`, `diff`, `stats`, `restart` methods (~150 lines total)
- `src/sandbox_mcp/sandbox_env.py` — add 5 `_op_docker_X` dispatchers (~50 lines total); add 5 entries to `DOCKER_HELP_RESPONSE` (~70 lines total)
- `tests/test_docker_backend.py` — add ~30 unit tests for the 5 backend methods
- `tests/test_sandbox_env.py` — add ~10 dispatcher tests + extend `test_docker_help_returns_docker_ops`

No new files. No new dependencies. No config changes. No schema migrations.

---

## Task 1: Backend `inspect()` — curated view

**Files:**
- Modify: `src/sandbox_mcp/backends/docker_backend.py` (add `inspect` method)
- Modify: `tests/test_docker_backend.py` (add `test_docker_inspect_*` cases)

- [ ] **Step 1: Write the failing test for curated inspect**

Add to `tests/test_docker_backend.py`:

```python
def test_docker_inspect_returns_curated_view(docker_backend, mock_client):
    """Curated inspect returns a flattened, agent-friendly dict with only
    fields `shell_exec` can't easily surface (state, cmd, mounts, labels,
    restart_policy).  Env / working_dir / user / network IP are deliberately
    omitted — those are `shell_exec env` / `pwd` / `whoami` / `hostname -i`.
    """
    container = mock_client.containers.get.return_value
    container.attrs = {
        "State": {
            "Status": "running",
            "Running": True,
            "ExitCode": 0,
            "Error": "",
            "RestartCount": 2,
            "StartedAt": "2026-07-16T10:00:01Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
            "Health": {"Status": "healthy"},
        },
        "Config": {
            "Image": "python:3.12-slim",
            "Cmd": ["python", "-m", "http.server"],
            "Entrypoint": None,
            "Labels": {
                "sandbox-mcp.managed": "true",
                "sandbox-mcp.machine": "dev",
                "sandbox-mcp.purpose": "Python dev",
            },
        },
        "Created": "2026-07-16T10:00:00Z",
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}},
        "Mounts": [
            {"Source": "/host/x", "Destination": "/workspace", "Mode": "rw"},
            {"Source": "/host/share", "Destination": "/share", "Mode": "ro"},
        ],
    }
    container.short_id = "abc123def456"

    result = docker_backend.inspect("dev")

    assert result["id"] == "abc123def456"
    assert result["name"] == "dev"
    assert result["image"] == "python:3.12-slim"
    assert result["created"] == "2026-07-16T10:00:00Z"
    assert result["started_at"] == "2026-07-16T10:00:01Z"
    assert result["finished_at"] == "0001-01-01T00:00:00Z"
    assert result["state"]["status"] == "running"
    assert result["state"]["running"] is True
    assert result["state"]["exit_code"] == 0
    assert result["state"]["restart_count"] == 2
    assert result["state"]["health"] == "healthy"
    assert result["cmd"] == ["python", "-m", "http.server"]
    assert result["entrypoint"] is None
    assert result["mounts"] == [
        {"source": "/host/x", "destination": "/workspace", "mode": "rw"},
        {"source": "/host/share", "destination": "/share", "mode": "ro"},
    ]
    assert result["labels"]["sandbox-mcp.purpose"] == "Python dev"
    assert result["restart_policy"] == {"name": "unless-stopped", "max_retry": 0}
    # Deliberately omitted (shell_exec can answer):
    assert "env" not in result
    assert "working_dir" not in result
    assert "user" not in result
    assert "network" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_docker_backend.py::test_docker_inspect_returns_curated_view -v`
Expected: FAIL with `AttributeError: 'DockerBackend' object has no attribute 'inspect'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/sandbox_mcp/backends/docker_backend.py` (insert after `commit()`, before `build()`):

```python
    def inspect(self, name: str, raw: bool = False) -> dict:
        """Return curated container config, or full ``attrs`` when ``raw=True``.

        Curated view deliberately omits ``Config.Env``, ``Config.WorkingDir``,
        ``Config.User``, and ``NetworkSettings.IPAddress`` — agents get
        those from :func:`sandbox_shell_exec` (``env`` / ``pwd`` / ``whoami``
        / ``hostname -i``).  The curated set focuses on what ``shell_exec``
        cannot answer: state, cmd, mounts, labels, restart policy.
        """
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
            container.reload()
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return {"error": str(e.explanation if hasattr(e, "explanation") else e),
                    "status": "error"}

        if raw:
            return container.attrs

        state = (container.attrs.get("State") or {})
        config = (container.attrs.get("Config") or {})
        host_cfg = (container.attrs.get("HostConfig") or {})
        restart = host_cfg.get("RestartPolicy") or {}
        health = state.get("Health") or {}

        return {
            "id": container.short_id,
            "name": name,
            "image": config.get("Image", ""),
            "created": container.attrs.get("Created", ""),
            "started_at": state.get("StartedAt", ""),
            "finished_at": state.get("FinishedAt", ""),
            "state": {
                "status": state.get("Status", "unknown"),
                "running": bool(state.get("Running", False)),
                "exit_code": state.get("ExitCode", 0),
                "error": state.get("Error", "") or "",
                "restart_count": state.get("RestartCount", 0),
                "health": health.get("Status", "") or "",
            },
            "cmd": list(config.get("Cmd") or []) or None,
            "entrypoint": list(config.get("Entrypoint") or []) or None,
            "mounts": [
                {
                    "source": m.get("Source", ""),
                    "destination": m.get("Destination", ""),
                    "mode": m.get("Mode", ""),
                }
                for m in (container.attrs.get("Mounts") or [])
            ],
            "labels": dict(config.get("Labels") or {}),
            "restart_policy": {
                "name": restart.get("Name", ""),
                "max_retry": restart.get("MaximumRetryCount", 0),
            },
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_docker_backend.py::test_docker_inspect_returns_curated_view -v`
Expected: PASS

- [ ] **Step 5: Add test for raw=True escape hatch**

```python
def test_docker_inspect_raw_returns_full_attrs(docker_backend, mock_client):
    """raw=True returns the unfiltered attrs dict — agent can read every
    key including Config.Env values and full NetworkSettings."""
    container = mock_client.containers.get.return_value
    container.attrs = {
        "State": {"Status": "running"},
        "Config": {
            "Image": "alpine:3",
            "Env": ["POSTGRES_PASSWORD=hunter2"],
            "Cmd": ["sleep", "infinity"],
        },
    }

    result = docker_backend.inspect("dev", raw=True)

    assert result is container.attrs
    assert result["Config"]["Env"] == ["POSTGRES_PASSWORD=hunter2"]
```

- [ ] **Step 6: Run the new test**

Run: `pytest tests/test_docker_backend.py::test_docker_inspect_raw_returns_full_attrs -v`
Expected: PASS

- [ ] **Step 7: Add test for container not found**

```python
def test_docker_inspect_container_not_found(docker_backend, mock_client):
    """Container that disappears between calls returns error dict, not raise."""
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.get.side_effect = DockerNotFound("not here")

    result = docker_backend.inspect("ghost")

    assert result["status"] == "error"
    assert "not here" in result["error"]
```

- [ ] **Step 8: Run the new test**

Run: `pytest tests/test_docker_backend.py::test_docker_inspect_container_not_found -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add src/sandbox_mcp/backends/docker_backend.py tests/test_docker_backend.py
git commit -m "feat(docker): backend inspect() — curated view + raw escape hatch"
```

---

## Task 2: Backend `logs()` — tail/since/until/timestamps

**Files:**
- Modify: `src/sandbox_mcp/backends/docker_backend.py` (add `logs` method)
- Modify: `tests/test_docker_backend.py` (add `test_docker_logs_*` cases)

- [ ] **Step 1: Write failing test for default-tail behaviour**

Add to `tests/test_docker_backend.py`:

```python
def test_docker_logs_default_tail_200(docker_backend, mock_client):
    """Default tail is 200 lines."""
    container = mock_client.containers.get.return_value
    container.logs.return_value = b"line1\nline2\n"

    result = docker_backend.logs("dev")

    container.logs.assert_called_once_with(
        tail=200, since=None, until=None, timestamps=False
    )
    assert result["logs"] == "line1\nline2\n"
    assert result["truncated"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_docker_backend.py::test_docker_logs_default_tail_200 -v`
Expected: FAIL with `AttributeError: 'DockerBackend' object has no attribute 'logs'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/sandbox_mcp/backends/docker_backend.py` (after `inspect()`):

```python
    def logs(
        self,
        name: str,
        *,
        tail: int = 200,
        since: str | None = None,
        until: str | None = None,
        timestamps: bool = False,
    ) -> dict:
        """Read container logs (one-shot, merged stdout+stderr).

        ``tail`` is capped at 10000 to prevent token-bombing a single
        response.  ``since`` / ``until`` accept RFC 3339 timestamps or
        relative durations (``"10m"``, ``"1h"``) — both formats are
        accepted by the docker daemon.

        Works against stopped containers: docker keeps the log buffer
        past exit, which is the primary use case (read why a container
        died).
        """
        if not isinstance(tail, int) or tail < 1 or tail > 10000:
            return {
                "error": f"tail must be between 1 and 10000, got {tail!r}",
                "status": "error",
            }
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return {"error": str(e.explanation if hasattr(e, "explanation") else e),
                    "status": "error"}

        try:
            raw = container.logs(
                tail=tail, since=since, until=until, timestamps=timestamps
            )
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return {"error": str(e.explanation if hasattr(e, "explanation") else e),
                    "status": "error"}

        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw or "")
        # Truncated iff the daemon returned tail-many *or more* lines
        # (we requested exactly tail, and we got tail).  Conservative:
        # mark truncated when text ends with newline + non-empty body
        # AND the request actually limited.  This is a soft heuristic —
        # exact line counts from the daemon aren't cheap.
        truncated = bool(text) and tail < 10**9
        return {"logs": text, "truncated": truncated}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_docker_backend.py::test_docker_logs_default_tail_200 -v`
Expected: PASS

- [ ] **Step 5: Add test for parameters passed through**

```python
def test_docker_logs_passes_through_filters(docker_backend, mock_client):
    """since/until/timestamps/tail flow through to the SDK."""
    container = mock_client.containers.get.return_value
    container.logs.return_value = b""

    docker_backend.logs(
        "dev",
        tail=50,
        since="2026-07-16T10:00:00Z",
        until="10m",
        timestamps=True,
    )

    container.logs.assert_called_once_with(
        tail=50,
        since="2026-07-16T10:00:00Z",
        until="10m",
        timestamps=True,
    )
```

- [ ] **Step 6: Run the new test**

Run: `pytest tests/test_docker_backend.py::test_docker_logs_passes_through_filters -v`
Expected: PASS

- [ ] **Step 7: Add tests for tail cap and not-found**

```python
def test_docker_logs_rejects_tail_above_cap(docker_backend, mock_client):
    """Tail > 10000 is rejected with error (prevents token-bombing)."""
    result = docker_backend.logs("dev", tail=99999)
    assert result["status"] == "error"
    assert "10000" in result["error"]


def test_docker_logs_rejects_tail_below_one(docker_backend, mock_client):
    result = docker_backend.logs("dev", tail=0)
    assert result["status"] == "error"


def test_docker_logs_container_not_found(docker_backend, mock_client):
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.get.side_effect = DockerNotFound("nope")

    result = docker_backend.logs("ghost")
    assert result["status"] == "error"


def test_docker_logs_decodes_bytes_with_replacement(docker_backend, mock_client):
    """Garbage bytes don't crash — utf-8 with errors='replace'."""
    container = mock_client.containers.get.return_value
    container.logs.return_value = b"good \xff\xfe bad"

    result = docker_backend.logs("dev")
    assert "good" in result["logs"]
    assert "bad" in result["logs"]
```

- [ ] **Step 8: Run the new tests**

Run: `pytest tests/test_docker_backend.py -k docker_logs -v`
Expected: 5 PASS

- [ ] **Step 9: Commit**

```bash
git add src/sandbox_mcp/backends/docker_backend.py tests/test_docker_backend.py
git commit -m "feat(docker): backend logs() with tail/since/until/timestamps"
```

---

## Task 3: Backend `diff()` — A/C/D grouping

**Files:**
- Modify: `src/sandbox_mcp/backends/docker_backend.py` (add `diff` method)
- Modify: `tests/test_docker_backend.py` (add `test_docker_diff_*` cases)

- [ ] **Step 1: Write failing test for grouping**

Add to `tests/test_docker_backend.py`:

```python
def test_docker_diff_groups_by_kind(docker_backend, mock_client):
    """diff() returns changes grouped by A (added) / C (changed) / D (deleted)."""
    container = mock_client.containers.get.return_value
    container.diff.return_value = [
        {"Path": "/workspace/new.txt", "Kind": 1},   # Added
        {"Path": "/workspace/modified.yaml", "Kind": 0},  # Changed
        {"Path": "/workspace/old.log", "Kind": 2},   # Deleted
        {"Path": "/workspace/another_new.txt", "Kind": 1},  # Added
    ]

    result = docker_backend.diff("dev")

    assert result["changes"]["A"] == ["/workspace/another_new.txt", "/workspace/new.txt"]
    assert result["changes"]["C"] == ["/workspace/modified.yaml"]
    assert result["changes"]["D"] == ["/workspace/old.log"]
    assert result["summary"] == {"added": 2, "changed": 1, "deleted": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_docker_backend.py::test_docker_diff_groups_by_kind -v`
Expected: FAIL with `AttributeError: 'DockerBackend' object has no attribute 'diff'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/sandbox_mcp/backends/docker_backend.py` (after `logs()`):

```python
    def diff(self, name: str) -> dict:
        """Filesystem changes vs the container's image, grouped A/C/D.

        docker SDK ``Container.diff()`` returns
        ``[{"Path": str, "Kind": int}, ...]`` where Kind is
        ``0=Modified``, ``1=Added``, ``2=Deleted``.
        """
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
            raw = container.diff()
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return {"error": str(e.explanation if hasattr(e, "explanation") else e),
                    "status": "error"}

        added, changed, deleted = [], [], []
        for entry in raw or []:
            path = entry.get("Path", "")
            kind = entry.get("Kind")
            if kind == 0:
                changed.append(path)
            elif kind == 1:
                added.append(path)
            elif kind == 2:
                deleted.append(path)

        added.sort()
        changed.sort()
        deleted.sort()

        return {
            "changes": {"A": added, "C": changed, "D": deleted},
            "summary": {"added": len(added), "changed": len(changed), "deleted": len(deleted)},
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_docker_backend.py::test_docker_diff_groups_by_kind -v`
Expected: PASS

- [ ] **Step 5: Add tests for empty diff and not-found**

```python
def test_docker_diff_empty_returns_empty_groups(docker_backend, mock_client):
    """A clean container (no fs changes) returns three empty groups."""
    container = mock_client.containers.get.return_value
    container.diff.return_value = []

    result = docker_backend.diff("dev")

    assert result["changes"] == {"A": [], "C": [], "D": []}
    assert result["summary"] == {"added": 0, "changed": 0, "deleted": 0}


def test_docker_diff_container_not_found(docker_backend, mock_client):
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.get.side_effect = DockerNotFound("nope")

    result = docker_backend.diff("ghost")
    assert result["status"] == "error"
```

- [ ] **Step 6: Run the new tests**

Run: `pytest tests/test_docker_backend.py -k docker_diff -v`
Expected: 3 PASS

- [ ] **Step 7: Commit**

```bash
git add src/sandbox_mcp/backends/docker_backend.py tests/test_docker_backend.py
git commit -m "feat(docker): backend diff() — A/C/D filesystem change groups"
```

---

## Task 4: Backend `stats()` — one-shot CPU/mem/net/block

**Files:**
- Modify: `src/sandbox_mcp/backends/docker_backend.py` (add `stats` method)
- Modify: `tests/test_docker_backend.py` (add `test_docker_stats_*` cases)

- [ ] **Step 1: Write failing test for snapshot**

Add to `tests/test_docker_backend.py`:

```python
def test_docker_stats_returns_snapshot(docker_backend, mock_client):
    """stats() returns a curated one-shot snapshot — no streaming."""
    container = mock_client.containers.get.return_value
    container.stats.return_value = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200_000_000, "percpu_usage": [100_000_000, 100_000_000]},
            "system_cpu_usage": 10_000_000_000,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100_000_000},
            "system_cpu_usage": 9_900_000_000,
        },
        "memory_stats": {"usage": 50 * 1024 * 1024, "limit": 1024 * 1024 * 1024},
        "networks": {
            "eth0": {"rx_bytes": 1234, "tx_bytes": 5678},
            "eth1": {"rx_bytes": 100, "tx_bytes": 200},
        },
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "Read", "value": 4096},
                {"op": "Write", "value": 8192},
            ]
        },
    }

    result = docker_backend.stats("dev")

    container.stats.assert_called_once_with(stream=False)
    # CPU: cpu_delta=100_000_000, system_delta=100_000_000, num_cpus=2
    # cpu% = (100e6 / 100e6) * 2 * 100 = 200%
    assert result["cpu_percent"] == pytest.approx(200.0)
    # Memory
    assert result["memory"]["usage_bytes"] == 50 * 1024 * 1024
    assert result["memory"]["limit_bytes"] == 1024 * 1024 * 1024
    assert result["memory"]["usage_percent"] == pytest.approx(50 * 1024 * 1024 / (1024 * 1024 * 1024) * 100)
    # Network — aggregated across interfaces
    assert result["network"]["rx_bytes"] == 1234 + 100
    assert result["network"]["tx_bytes"] == 5678 + 200
    # Block IO — split by op
    assert result["block_io"]["read_bytes"] == 4096
    assert result["block_io"]["write_bytes"] == 8192
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_docker_backend.py::test_docker_stats_returns_snapshot -v`
Expected: FAIL with `AttributeError: 'DockerBackend' object has no attribute 'stats'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/sandbox_mcp/backends/docker_backend.py` (after `diff()`):

```python
    def stats(self, name: str, *, stream: bool = False) -> dict:
        """One-shot resource snapshot.  Streaming is explicitly refused —
        the MCP tool-call model is request/response; live monitoring is
        a loop of repeated ``stats()`` calls.
        """
        if stream:
            return {
                "error": "streaming is not supported; call docker_stats again for the next snapshot",
                "status": "error",
            }
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
            raw = container.stats(stream=False)
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return {"error": str(e.explanation if hasattr(e, "explanation") else e),
                    "status": "error"}

        cpu = self._compute_cpu_percent(raw)
        mem = self._compute_memory(raw)
        net = self._compute_network(raw)
        blk = self._compute_block_io(raw)

        return {
            "cpu_percent": cpu,
            "memory": mem,
            "network": net,
            "block_io": blk,
        }

    @staticmethod
    def _compute_cpu_percent(raw: dict) -> float:
        """Standard docker CPU% formula in single-snapshot form.

        ``cpu_delta / system_delta * num_cpus * 100``; returns 0 when
        system_delta is 0 (first sample or zero-elapsed case).
        """
        cpu_stats = raw.get("cpu_stats") or {}
        precpu_stats = raw.get("precpu_stats") or {}
        cpu_usage = cpu_stats.get("cpu_usage") or {}
        precpu_usage = precpu_stats.get("cpu_usage") or {}
        cpu_delta = (cpu_usage.get("total_usage") or 0) - (precpu_usage.get("total_usage") or 0)
        system_delta = (cpu_stats.get("system_cpu_usage") or 0) - (precpu_stats.get("system_cpu_usage") or 0)
        if system_delta <= 0 or cpu_delta < 0:
            return 0.0
        num_cpus = (
            cpu_stats.get("online_cpus")
            or len(cpu_usage.get("percpu_usage") or [])
            or 1
        )
        return (cpu_delta / system_delta) * num_cpus * 100.0

    @staticmethod
    def _compute_memory(raw: dict) -> dict:
        mem = raw.get("memory_stats") or {}
        usage = mem.get("usage") or 0
        limit = mem.get("limit") or 0
        pct = (usage / limit * 100.0) if limit else 0.0
        return {"usage_bytes": usage, "limit_bytes": limit, "usage_percent": pct}

    @staticmethod
    def _compute_network(raw: dict) -> dict:
        nets = raw.get("networks") or {}
        rx = sum((iface.get("rx_bytes") or 0) for iface in nets.values())
        tx = sum((iface.get("tx_bytes") or 0) for iface in nets.values())
        return {"rx_bytes": rx, "tx_bytes": tx}

    @staticmethod
    def _compute_block_io(raw: dict) -> dict:
        blkio = raw.get("blkio_stats") or {}
        entries = blkio.get("io_service_bytes_recursive") or []
        read_bytes = sum((e.get("value") or 0) for e in entries if e.get("op") == "Read")
        write_bytes = sum((e.get("value") or 0) for e in entries if e.get("op") == "Write")
        return {"read_bytes": read_bytes, "write_bytes": write_bytes}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_docker_backend.py::test_docker_stats_returns_snapshot -v`
Expected: PASS

- [ ] **Step 5: Add tests for stream rejection and not-found**

```python
def test_docker_stats_rejects_streaming(docker_backend, mock_client):
    """stream=True is refused: MCP tool-call model doesn't fit long-lived streams."""
    result = docker_backend.stats("dev", stream=True)
    assert result["status"] == "error"
    assert "streaming is not supported" in result["error"]
    mock_client.containers.get.assert_not_called()


def test_docker_stats_container_not_found(docker_backend, mock_client):
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.get.side_effect = DockerNotFound("nope")

    result = docker_backend.stats("ghost")
    assert result["status"] == "error"


def test_docker_stats_handles_zero_cpu_delta(docker_backend, mock_client):
    """First sample has zero deltas; cpu_percent is 0.0 (no divide-by-zero)."""
    container = mock_client.containers.get.return_value
    container.stats.return_value = {
        "cpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
        "precpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
        "memory_stats": {},
        "networks": {},
        "blkio_stats": {},
    }

    result = docker_backend.stats("dev")
    assert result["cpu_percent"] == 0.0
    assert result["memory"] == {"usage_bytes": 0, "limit_bytes": 0, "usage_percent": 0.0}
    assert result["network"] == {"rx_bytes": 0, "tx_bytes": 0}
    assert result["block_io"] == {"read_bytes": 0, "write_bytes": 0}
```

- [ ] **Step 6: Run the new tests**

Run: `pytest tests/test_docker_backend.py -k docker_stats -v`
Expected: 4 PASS

- [ ] **Step 7: Commit**

```bash
git add src/sandbox_mcp/backends/docker_backend.py tests/test_docker_backend.py
git commit -m "feat(docker): backend stats() — one-shot CPU/memory/network/block"
```

---

## Task 5: Backend `restart()` — atomic stop+start

**Files:**
- Modify: `src/sandbox_mcp/backends/docker_backend.py` (add `restart` method)
- Modify: `tests/test_docker_backend.py` (add `test_docker_restart_*` cases)

- [ ] **Step 1: Write failing test for successful restart**

Add to `tests/test_docker_backend.py`:

```python
def test_docker_restart_succeeds(docker_backend, mock_client):
    """A clean restart reports 'running' and uses the default 10s timeout."""
    container = mock_client.containers.get.return_value
    container.attrs = {"State": {"Status": "running", "Running": True}}

    info = docker_backend.restart("dev")

    container.restart.assert_called_once_with(timeout=10)
    assert info.name == "dev"
    assert info.status == "running"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_docker_backend.py::test_docker_restart_succeeds -v`
Expected: FAIL with `AttributeError: 'DockerBackend' object has no attribute 'restart'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/sandbox_mcp/backends/docker_backend.py` (after `stats()` and helpers, near `start()`):

```python
    def restart(self, name: str, timeout: int = 10) -> TargetInfo:
        """Atomic restart: stop then start, then verify the container
        actually stayed up.  A crashing command exits within ms of
        ``restart()`` returning — we re-check ``State.Status`` and
        surface a diagnostic if the container died, matching the
        ``start()`` semantics.
        """
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
            container.restart(timeout=timeout)
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return TargetInfo(name=name, backend="docker", status="error", error=str(e))

        # Use _running_info to confirm the container actually came back.
        info = self._running_info(container, name)
        return info
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_docker_backend.py::test_docker_restart_succeeds -v`
Expected: PASS

- [ ] **Step 5: Add tests for timeout, not-found, container-died-after-restart**

```python
def test_docker_restart_passes_timeout(docker_backend, mock_client):
    """Custom timeout flows to container.restart()."""
    container = mock_client.containers.get.return_value
    container.attrs = {"State": {"Status": "running", "Running": True}}

    docker_backend.restart("dev", timeout=30)
    container.restart.assert_called_once_with(timeout=30)


def test_docker_restart_container_not_found(docker_backend, mock_client):
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.get.side_effect = DockerNotFound("nope")

    info = docker_backend.restart("ghost")
    assert info.status == "error"


def test_docker_restart_surfaces_crash_diagnostic(docker_backend, mock_client):
    """Container whose CMD crashes post-restart reports error + log tail,
    not 'running' (matches _running_info post-check semantics)."""
    container = mock_client.containers.get.return_value
    container.attrs = {"State": {"Status": "exited", "ExitCode": 127}}
    container.logs.return_value = b"sleep: command not found\n"

    info = docker_backend.restart("dev")

    assert info.status == "error"
    assert "exited" in info.error
    assert "exit_code=127" in info.error
    assert "sleep: command not found" in info.error
```

- [ ] **Step 6: Run the new tests**

Run: `pytest tests/test_docker_backend.py -k docker_restart -v`
Expected: 4 PASS

- [ ] **Step 7: Commit**

```bash
git add src/sandbox_mcp/backends/docker_backend.py tests/test_docker_backend.py
git commit -m "feat(docker): backend restart() — atomic stop+start with post-check"
```

---

## Task 6: SandboxEnv dispatchers (all 5) — TDD

**Files:**
- Modify: `src/sandbox_mcp/sandbox_env.py` (add 5 `_op_docker_X` methods + import)
- Modify: `tests/test_sandbox_env.py` (add dispatcher tests)

- [ ] **Step 1: Write failing dispatcher tests**

Add to `tests/test_sandbox_env.py`:

```python
def test_docker_inspect_dispatches_to_backend(sandbox_env):
    """docker_inspect resolves machine, validates backend, calls backend.inspect."""
    from sandbox_mcp.backends.docker_backend import DockerBackend

    backend = MagicMock(spec=DockerBackend)
    backend.inspect.return_value = {"id": "abc", "name": "dev"}
    sandbox_env._machines.resolve_machine.return_value = "dev"
    sandbox_env._machines.get_backend.return_value = backend

    result = sandbox_env.dispatch("docker_inspect", {"machine": "dev"})

    sandbox_env._machines.resolve_machine.assert_called_once_with("dev")
    backend.inspect.assert_called_once_with("dev", raw=False)
    assert result == {"id": "abc", "name": "dev"}


def test_docker_inspect_passes_raw_flag(sandbox_env):
    from sandbox_mcp.backends.docker_backend import DockerBackend

    backend = MagicMock(spec=DockerBackend)
    backend.inspect.return_value = {"State": {}}
    sandbox_env._machines.resolve_machine.return_value = "dev"
    sandbox_env._machines.get_backend.return_value = backend

    sandbox_env.dispatch("docker_inspect", {"machine": "dev", "raw": True})

    backend.inspect.assert_called_once_with("dev", raw=True)


def test_docker_inspect_rejects_non_docker_machine(sandbox_env):
    """SSH backend returns 'docker_inspect only supported on Docker machines'."""
    sandbox_env._machines.resolve_machine.return_value = "remote"
    sandbox_env._machines.get_backend.return_value = sandbox_env._ssh  # SSHBackend instance

    result = sandbox_env.dispatch("docker_inspect", {"machine": "remote"})

    assert "error" in result
    assert "Docker machines" in result["error"]


def test_docker_logs_dispatches_to_backend(sandbox_env):
    from sandbox_mcp.backends.docker_backend import DockerBackend

    backend = MagicMock(spec=DockerBackend)
    backend.logs.return_value = {"logs": "x\n", "truncated": False}
    sandbox_env._machines.resolve_machine.return_value = "dev"
    sandbox_env._machines.get_backend.return_value = backend

    result = sandbox_env.dispatch(
        "docker_logs",
        {"machine": "dev", "tail": 50, "since": "10m", "timestamps": True},
    )

    backend.logs.assert_called_once_with(
        "dev", tail=50, since="10m", until=None, timestamps=True
    )
    assert result == {"logs": "x\n", "truncated": False}


def test_docker_diff_dispatches_to_backend(sandbox_env):
    from sandbox_mcp.backends.docker_backend import DockerBackend

    backend = MagicMock(spec=DockerBackend)
    backend.diff.return_value = {"changes": {"A": [], "C": [], "D": []}, "summary": {}}
    sandbox_env._machines.resolve_machine.return_value = "dev"
    sandbox_env._machines.get_backend.return_value = backend

    result = sandbox_env.dispatch("docker_diff", {"machine": "dev"})

    backend.diff.assert_called_once_with("dev")
    assert "changes" in result


def test_docker_stats_dispatches_to_backend(sandbox_env):
    from sandbox_mcp.backends.docker_backend import DockerBackend

    backend = MagicMock(spec=DockerBackend)
    backend.stats.return_value = {"cpu_percent": 1.0, "memory": {}, "network": {}, "block_io": {}}
    sandbox_env._machines.resolve_machine.return_value = "dev"
    sandbox_env._machines.get_backend.return_value = backend

    result = sandbox_env.dispatch("docker_stats", {"machine": "dev"})

    backend.stats.assert_called_once_with("dev", stream=False)
    assert result["cpu_percent"] == 1.0


def test_docker_restart_dispatches_to_backend(sandbox_env):
    from sandbox_mcp.backends.docker_backend import DockerBackend
    from sandbox_mcp.backends.base import TargetInfo

    backend = MagicMock(spec=DockerBackend)
    backend.restart.return_value = TargetInfo(name="dev", backend="docker", status="running")
    sandbox_env._machines.resolve_machine.return_value = "dev"
    sandbox_env._machines.get_backend.return_value = backend

    result = sandbox_env.dispatch("docker_restart", {"machine": "dev", "timeout": 30})

    backend.restart.assert_called_once_with("dev", timeout=30)
    assert result["status"] == "running"


def test_docker_logs_requires_machine(sandbox_env):
    """All five actions require a 'machine' param — verified for docker_logs
    as the representative case (others use the same _require helper)."""
    result = sandbox_env.dispatch("docker_logs", {})
    assert "error" in result
    assert "machine" in result["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sandbox_env.py -k "docker_inspect_dispatches_to_backend or docker_logs_dispatches_to_backend or docker_diff_dispatches_to_backend or docker_stats_dispatches_to_backend or docker_restart_dispatches_to_backend" -v`
Expected: All FAIL (Unknown action: docker_inspect / etc.)

- [ ] **Step 3: Write dispatcher implementations**

Add to `src/sandbox_mcp/sandbox_env.py` (insert before `# ---- ssh ----`):

```python
    # ---- docker introspection ----

    def _op_docker_inspect(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend

        if not isinstance(backend, DockerBackend):
            return {"error": "docker_inspect only supported on Docker machines"}
        return backend.inspect(machine, raw=bool(params.get("raw", False)))

    def _op_docker_logs(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend

        if not isinstance(backend, DockerBackend):
            return {"error": "docker_logs only supported on Docker machines"}
        return backend.logs(
            machine,
            tail=int(params.get("tail", 200)),
            since=params.get("since"),
            until=params.get("until"),
            timestamps=bool(params.get("timestamps", False)),
        )

    def _op_docker_diff(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend

        if not isinstance(backend, DockerBackend):
            return {"error": "docker_diff only supported on Docker machines"}
        return backend.diff(machine)

    def _op_docker_stats(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend

        if not isinstance(backend, DockerBackend):
            return {"error": "docker_stats only supported on Docker machines"}
        return backend.stats(machine, stream=bool(params.get("stream", False)))

    def _op_docker_restart(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend

        if not isinstance(backend, DockerBackend):
            return {"error": "docker_restart only supported on Docker machines"}
        info = backend.restart(machine, timeout=int(params.get("timeout", 10)))
        return {"machine": info.name, "status": info.status, **({"error": info.error} if info.error else {})}
```

- [ ] **Step 4: Run the dispatcher tests**

Run: `pytest tests/test_sandbox_env.py -k "docker_inspect_dispatches_to_backend or docker_logs_dispatches_to_backend or docker_diff_dispatches_to_backend or docker_stats_dispatches_to_backend or docker_restart_dispatches_to_backend or docker_inspect_passes_raw_flag or docker_inspect_rejects_non_docker_machine or docker_logs_requires_machine" -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add src/sandbox_mcp/sandbox_env.py tests/test_sandbox_env.py
git commit -m "feat(env): dispatchers for docker_inspect/logs/diff/stats/restart"
```

---

## Task 7: Update DOCKER_HELP_RESPONSE

**Files:**
- Modify: `src/sandbox_mcp/sandbox_env.py` (extend `DOCKER_HELP_RESPONSE["operations"]`)
- Modify: `tests/test_sandbox_env.py` (extend `test_docker_help_returns_docker_ops`)

- [ ] **Step 1: Update the help test first**

Replace `test_docker_help_returns_docker_ops` in `tests/test_sandbox_env.py` with:

```python
def test_docker_help_returns_docker_ops(sandbox_env):
    result = sandbox_env.dispatch("docker_help", {})
    actions = [op["action"] for op in result["operations"]]
    # Lifecycle
    assert "docker_run" in actions
    assert "docker_build" in actions
    assert "docker_commit" in actions
    assert "docker_stop" in actions
    assert "docker_start" in actions
    assert "docker_remove" in actions
    # Discovery
    assert "docker_ps" in actions
    assert "docker_images" in actions
    # Introspection (new in this change)
    assert "docker_inspect" in actions
    assert "docker_logs" in actions
    assert "docker_diff" in actions
    assert "docker_stats" in actions
    assert "docker_restart" in actions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sandbox_env.py::test_docker_help_returns_docker_ops -v`
Expected: FAIL with `assert 'docker_inspect' in actions` (and 4 more)

- [ ] **Step 3: Add help entries to DOCKER_HELP_RESPONSE**

In `src/sandbox_mcp/sandbox_env.py`, append to `DOCKER_HELP_RESPONSE["operations"]` (before the closing `]` of the list):

```python
        {
            "action": "docker_inspect",
            "description": (
                "Curated container config: state, image, cmd, entrypoint, "
                "mounts (source host path -> bind -> mode), labels, restart "
                "policy.  Deliberately omits Env/working_dir/user/network "
                "(use shell_exec env/pwd/whoami/hostname -i for those). "
                "Pass raw=true to get the full attrs dict (incl. Env values, "
                "NetworkSettings, HostConfig)."
            ),
            "required": {"machine": "string"},
            "optional": {"raw": "bool - default false"},
            "returns": {"id": "string", "name": "string", "image": "string",
                        "state": "object", "cmd": "list|null",
                        "entrypoint": "list|null", "mounts": "list",
                        "labels": "object", "restart_policy": "object"},
        },
        {
            "action": "docker_logs",
            "description": (
                "Read container logs (one-shot, merged stdout+stderr). "
                "Works on stopped containers (primary use: read why a "
                "container died).  tail is hard-capped at 10000."
            ),
            "required": {"machine": "string"},
            "optional": {
                "tail": "int - default 200, max 10000",
                "since": "string - ISO 8601 or relative (e.g. 10m)",
                "until": "string - same format as since",
                "timestamps": "bool - prefix each line with RFC 3339 ts",
            },
            "returns": {"logs": "string", "truncated": "bool"},
        },
        {
            "action": "docker_diff",
            "description": (
                "Filesystem changes vs the container's image, grouped by "
                "kind: A (added), C (changed), D (deleted).  Useful before "
                "docker_commit to confirm 'this is the layer I want to save'."
            ),
            "required": {"machine": "string"},
            "returns": {
                "changes": {"A": "list of paths", "C": "list", "D": "list"},
                "summary": {"added": "int", "changed": "int", "deleted": "int"},
            },
        },
        {
            "action": "docker_stats",
            "description": (
                "One-shot resource snapshot: cpu_percent, memory usage/limit, "
                "network rx/tx (aggregated across all interfaces), block IO "
                "read/write.  Streaming is rejected (MCP tool-call model "
                "is request/response; call again for the next snapshot)."
            ),
            "required": {"machine": "string"},
            "optional": {"stream": "bool - default false; rejected if true"},
            "returns": {"cpu_percent": "number", "memory": "object",
                        "network": "object", "block_io": "object"},
        },
        {
            "action": "docker_restart",
            "description": (
                "Atomic restart (stop then start) with the same post-check "
                "as docker_start: a container whose CMD crashes is reported "
                "as 'error' with a diagnostic tail, not 'running'.  For a "
                "stopped container this is equivalent to docker_start."
            ),
            "required": {"machine": "string"},
            "optional": {"timeout": "int - default 10 (seconds for stop phase)"},
            "returns": {"machine": "string", "status": "running|error",
                        "error": "string (on failure)"},
        },
```

- [ ] **Step 4: Run the updated help test**

Run: `pytest tests/test_sandbox_env.py::test_docker_help_returns_docker_ops -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sandbox_mcp/sandbox_env.py tests/test_sandbox_env.py
git commit -m "feat(env): help text for docker_inspect/logs/diff/stats/restart"
```

---

## Task 8: Full verification

**Files:** none modified

- [ ] **Step 1: Run the full backend test file**

Run: `pytest tests/test_docker_backend.py -v`
Expected: All PASS (existing + new)

- [ ] **Step 2: Run the full sandbox_env test file**

Run: `pytest tests/test_sandbox_env.py -v`
Expected: All PASS (existing + new)

- [ ] **Step 3: Run the entire unit test suite**

Run: `pytest tests/ -v -m "not integration"`
Expected: All PASS

- [ ] **Step 4: Run lint**

Run: `ruff check src/sandbox_mcp/backends/docker_backend.py src/sandbox_mcp/sandbox_env.py tests/test_docker_backend.py tests/test_sandbox_env.py`
Expected: 0 errors (fix any inline)

- [ ] **Step 5: Verify the help text reads well end-to-end**

Run: `python -c "from sandbox_mcp.sandbox_env import DOCKER_HELP_RESPONSE; import json; print(json.dumps([op['action'] for op in DOCKER_HELP_RESPONSE['operations']], indent=2))"`
Expected: 13 actions listed (`docker_run`, `docker_build`, `docker_commit`, `docker_stop`, `docker_start`, `docker_remove`, `docker_ps`, `docker_images`, `docker_inspect`, `docker_logs`, `docker_diff`, `docker_stats`, `docker_restart`)

- [ ] **Step 6: Commit any lint fixes**

If step 4 produced changes:
```bash
git add -u
git commit -m "style: ruff fixes from lint"
```

(No commit if step 4 was clean.)

---

## Self-Review Checklist (run before declaring done)

1. **Spec coverage:**
   - `docker_inspect` curated view + raw → Task 1 ✓
   - `docker_logs` tail/since/until/timestamps + 10000 cap → Task 2 ✓
   - `docker_diff` A/C/D grouping → Task 3 ✓
   - `docker_stats` one-shot CPU/mem/net/block, stream rejection → Task 4 ✓
   - `docker_restart` atomic + post-check → Task 5 ✓
   - 5 dispatchers → Task 6 ✓
   - Help text updates → Task 7 ✓
   - Error: docker_X only supported on Docker machines → Task 6 ✓
   - Test plan coverage → Tasks 1-7 ✓

2. **Placeholder scan:** No TBD/TODO/"implement later" in the plan. Every step has actual code or test bodies.

3. **Type consistency:**
   - `inspect()` returns `dict` — used as-is in dispatcher ✓
   - `logs()` returns `{"logs": str, "truncated": bool}` — matches spec ✓
   - `diff()` returns `{"changes": {...}, "summary": {...}}` — matches spec ✓
   - `stats()` returns `{"cpu_percent", "memory", "network", "block_io"}` — matches spec ✓
   - `restart()` returns `TargetInfo` — dispatcher unwraps to `{"machine", "status", "error"?}` ✓