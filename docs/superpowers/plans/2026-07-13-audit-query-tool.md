# Self-Audit Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `sandbox_audit_query` MCP tool so the agent can self-audit its own actions, plus change the default `[audit] log_path` to a real file and conditionally expose the tool based on whether the log is file-backed.

**Architecture:** New query helpers (`read_tail_lines`, `parse_records`, `apply_filters`) live in `audit.py` next to the existing `AuditLogger`. A new `_handle_sandbox_audit_query` method on `SandboxServer` wires them into MCP. `list_tools()` conditionally appends the audit tool based on `cfg.audit.log_path`. Default `log_path` moves from `""` (stderr) to `"~/.sandbox-mcp/audit.log"`, gated by a drift-guard test.

**Tech Stack:** Python 3.12, stdlib `collections.deque` for tail-read, `pytest` for tests, `ruff` for lint.

**Spec:** `docs/superpowers/specs/2026-07-13-audit-query-tool-design.md`

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/sandbox_mcp/audit.py` | Add `read_tail_lines`, `parse_records`, `apply_filters`, plus `_MAX_TAIL` / `_DEFAULT_TAIL` constants |
| `src/sandbox_mcp/config.py` | Change `AuditConfig.log_path` default to `"~/.sandbox-mcp/audit.log"` |
| `src/sandbox_mcp/server.py` | Add `_AUDIT_QUERY_TOOL_DEFINITION` dict, conditional in `list_tools`, `_handle_sandbox_audit_query` method, startup log line in `main` + `main_http` |
| `config/config.example.toml` | Update `[audit]` section to reflect new default |
| `tests/test_audit_query.py` | New file: tests for query helpers + handler |
| `tests/test_server.py` | Update tool count test, add conditional exposure test |
| `tests/test_config.py` | No changes (drift-guard catches mismatched defaults automatically) |
| `README.md`, `README.zh.md` | Add `sandbox_audit_query` to tools table, update `[audit]` snippet |

---

## Task 1: Add query helpers to `audit.py`

**Files:**
- Modify: `src/sandbox_mcp/audit.py` (add helpers near the bottom, before `disable_audit`)
- Test: `tests/test_audit_query.py` (new)

### Step 1: Write the failing tests

Create `tests/test_audit_query.py`:

```python
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

import json

import pytest

from sandbox_mcp.audit import (
    _DEFAULT_TAIL,
    _MAX_TAIL,
    apply_filters,
    parse_records,
    read_tail_lines,
)


# ---------- read_tail_lines ----------

def test_read_tail_lines_returns_all_when_file_shorter_than_n(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    assert read_tail_lines(p, 100) == ["a\n", "b\n", "c\n"]


def test_read_tail_lines_returns_last_n_only(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("".join(f"line{i}\n" for i in range(10)), encoding="utf-8")
    assert read_tail_lines(p, 3) == ["line7\n", "line8\n", "line9\n"]


def test_read_tail_lines_empty_file(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("", encoding="utf-8")
    assert read_tail_lines(p, 10) == []


def test_read_tail_lines_rejects_zero():
    with pytest.raises(ValueError, match=r"tail must be in"):
        read_tail_lines.__wrapped__("/dev/null", 0) if hasattr(read_tail_lines, "__wrapped__") else None
    # Direct check:
    with pytest.raises(ValueError, match=r"tail must be in"):
        from pathlib import Path
        read_tail_lines(Path("/dev/null"), 0)


def test_read_tail_lines_rejects_negative(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"tail must be in"):
        read_tail_lines(p, -1)


def test_read_tail_lines_rejects_over_cap(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"tail must be in"):
        read_tail_lines(p, _MAX_TAIL + 1)


def test_read_tail_lines_accepts_cap(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("x\n", encoding="utf-8")
    assert read_tail_lines(p, _MAX_TAIL) == ["x\n"]


def test_read_tail_lines_handles_binary_gracefully(tmp_path):
    """``errors="replace"`` should keep the tool from crashing on bad bytes."""
    p = tmp_path / "a.log"
    p.write_bytes(b"good\n\xff\xfeline\n")
    lines = read_tail_lines(p, 10)
    assert lines[0] == "good\n"
    assert "line\n" in lines[1]


# ---------- parse_records ----------

def test_parse_records_skips_blank_lines():
    out = list(parse_records(["", "  ", "\n"]))
    assert out == []


def test_parse_records_yields_parsed_dicts():
    lines = [json.dumps({"ts": 1.0, "action": "x"}), json.dumps({"ts": 2.0, "action": "y"})]
    out = list(parse_records(lines))
    assert out == [{"ts": 1.0, "action": "x"}, {"ts": 2.0, "action": "y"}]


def test_parse_records_skips_malformed(caplog):
    lines = [
        json.dumps({"ts": 1.0, "action": "good"}),
        "this is not json",
        json.dumps({"ts": 2.0, "action": "good"}),
    ]
    with caplog.at_level("WARNING"):
        out = list(parse_records(lines))
    assert len(out) == 2
    assert any("malformed" in rec.lower() for rec in [r.message for r in caplog.records])


# ---------- apply_filters ----------

def _r(ts, action="shell_exec", machine="dev", status="ok"):
    return {"ts": ts, "action": action, "machine": machine, "status": status}


def test_apply_filters_no_filters_returns_all():
    recs = [_r(1.0), _r(2.0)]
    assert list(apply_filters(recs)) == recs


def test_apply_filters_action():
    recs = [_r(1.0, action="shell_exec"), _r(2.0, action="file_read")]
    out = list(apply_filters(recs, action="file_read"))
    assert len(out) == 1
    assert out[0]["action"] == "file_read"


def test_apply_filters_machine():
    recs = [_r(1.0, machine="a"), _r(2.0, machine="b")]
    out = list(apply_filters(recs, machine="b"))
    assert len(out) == 1
    assert out[0]["machine"] == "b"


def test_apply_filters_status():
    recs = [_r(1.0, status="ok"), _r(2.0, status="error")]
    out = list(apply_filters(recs, status="error"))
    assert len(out) == 1
    assert out[0]["status"] == "error"


def test_apply_filters_since_inclusive():
    recs = [_r(1.0), _r(2.0), _r(3.0)]
    out = list(apply_filters(recs, since=2.0))
    assert [r["ts"] for r in out] == [2.0, 3.0]


def test_apply_filters_until_exclusive():
    recs = [_r(1.0), _r(2.0), _r(3.0)]
    out = list(apply_filters(recs, until=2.0))
    assert [r["ts"] for r in out] == [1.0]


def test_apply_filters_combined():
    recs = [
        _r(1.0, action="shell_exec", machine="dev"),
        _r(2.0, action="file_read", machine="dev"),
        _r(3.0, action="shell_exec", machine="prod"),
    ]
    out = list(apply_filters(recs, action="shell_exec", machine="dev", since=1.5))
    assert [r["ts"] for r in out] == []


# ---------- constants ----------

def test_default_tail_is_5000():
    assert _DEFAULT_TAIL == 5000


def test_max_tail_is_100000():
    assert _MAX_TAIL == 100_000
```

### Step 2: Run the tests to confirm they fail

Run:
```bash
.venv/bin/python -m pytest tests/test_audit_query.py -v
```

Expected: every test FAILS with `ImportError: cannot import name 'read_tail_lines' from 'sandbox_mcp.audit'`.

### Step 3: Add the helpers to `audit.py`

In `src/sandbox_mcp/audit.py`, add these imports near the top (alongside the existing stdlib imports) and the helper functions + constants near the bottom (before `disable_audit`):

```python
import logging
from collections import deque
from collections.abc import Iterable, Iterator
```

(Keep the existing `from __future__ import annotations` line.)

Then just before `def disable_audit():`, insert:

```python
logger = logging.getLogger(__name__)

# Public bounds for ``sandbox_audit_query``'s ``tail`` parameter.
_DEFAULT_TAIL = 5_000
_MAX_TAIL = 100_000


def read_tail_lines(path: Path, n: int) -> list[str]:
    """Read at most ``n`` lines from the end of ``path``.

    Raises ``ValueError`` if ``n`` is outside ``(0, _MAX_TAIL]``.
    Binary-safe via ``errors="replace"``.
    """
    if n <= 0 or n > _MAX_TAIL:
        raise ValueError(f"tail must be in (0, {_MAX_TAIL}], got {n}")
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return list(deque(f, maxlen=n))


def parse_records(lines: Iterable[str]) -> Iterator[dict]:
    """Yield parsed JSON dicts from ``lines``; skip blanks and malformed input."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            logger.warning("audit: skipping malformed line: %r", line[:120])


def apply_filters(
    records: Iterable[dict],
    *,
    action: str | None = None,
    machine: str | None = None,
    status: str | None = None,
    since: float | None = None,
    until: float | None = None,
) -> Iterator[dict]:
    """Yield records that pass all non-None filters.

    ``since`` is inclusive (``ts >= since``); ``until`` is exclusive
    (``ts < until``).
    """
    for r in records:
        if action is not None and r.get("action") != action:
            continue
        if machine is not None and r.get("machine") != machine:
            continue
        if status is not None and r.get("status") != status:
            continue
        if since is not None and r.get("ts", 0) < since:
            continue
        if until is not None and r.get("ts", 0) >= until:
            continue
        yield r
```

### Step 4: Run the tests to confirm they pass

Run:
```bash
.venv/bin/python -m pytest tests/test_audit_query.py -v
```

Expected: all tests PASS.

### Step 5: Commit

```bash
git add src/sandbox_mcp/audit.py tests/test_audit_query.py
git commit -m "feat(audit): add tail-read + parse + filter helpers"
```

---

## Task 2: Change `AuditConfig.log_path` default + update example config

**Files:**
- Modify: `src/sandbox_mcp/config.py:75`
- Modify: `config/config.example.toml:43-47`

The drift-guard test `test_repo_example_matches_dataclass_defaults` in `tests/test_config.py` will fail if the dataclass default and `config.example.toml` disagree — both must move together.

### Step 1: Update the dataclass default

In `src/sandbox_mcp/config.py`, change line 75 from:

```python
    log_path: str = ""  # "" = stderr
```

to:

```python
    log_path: str = "~/.sandbox-mcp/audit.log"  # default file; "" = stderr
```

### Step 2: Update `config.example.toml`

In `config/config.example.toml`, change the `[audit]` section from:

```toml
[audit]
# JSON-line audit log destination.
#   "" (empty) = write to stderr (default)
#   "/path/to/file" = append to that file (parent dirs auto-created)
log_path = ""
```

to:

```toml
[audit]
# JSON-line audit log destination. Default: ~/.sandbox-mcp/audit.log
# (file-backed, enables the sandbox_audit_query self-audit tool).
#   "" (empty) = write to stderr (sandbox_audit_query will be hidden)
#   "/path/to/file" = append to that file (parent dirs auto-created)
log_path = "~/.sandbox-mcp/audit.log"
```

### Step 3: Run the config drift-guard test

Run:
```bash
.venv/bin/python -m pytest tests/test_config.py::test_repo_example_matches_dataclass_defaults -v
```

Expected: PASS (the example file matches the new default).

### Step 4: Run the full test suite

Run:
```bash
.venv/bin/python -m pytest -x -q
```

Expected: all 216 existing tests + new query-helper tests pass. `test_list_tools_returns_7` may fail (we'll fix in Task 3).

If `test_list_tools_returns_7` fails, that's expected and will be addressed in Task 3. Skip it for now by running:

```bash
.venv/bin/python -m pytest -q --deselect tests/test_server.py::test_list_tools_returns_7
```

Expected: PASS for everything else.

### Step 5: Commit

```bash
git add src/sandbox_mcp/config.py config/config.example.toml
git commit -m "feat(audit): default log_path to ~/.sandbox-mcp/audit.log"
```

---

## Task 3: Update `test_list_tools` for conditional exposure

**Files:**
- Modify: `tests/test_server.py`

The existing `test_list_tools_returns_7` will break because the new default makes the audit tool appear (count becomes 8). Fix it and add a new test for the conditional case.

### Step 1: Update the count test and add a conditional test

In `tests/test_server.py`, replace the entire `test_list_tools_returns_7` function (lines 31–41) with:

```python
def test_list_tools_includes_audit_query_by_default(server):
    """With the default config, audit is file-backed, so the tool is exposed."""
    tools = server.list_tools()
    names = {t.name for t in tools}
    expected = {
        "sandbox_shell_exec",
        "sandbox_shell_read",
        "sandbox_file_read",
        "sandbox_file_write",
        "sandbox_file_patch",
        "sandbox_file_search",
        "sandbox_env",
        "sandbox_audit_query",
    }
    assert expected.issubset(names)


def test_list_tools_omits_audit_query_when_log_path_empty(monkeypatch):
    """When [audit] log_path is empty, the audit tool is hidden from agents."""
    monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", "")
    from unittest.mock import patch
    with patch("sandbox_mcp.server.DockerBackend"), patch("sandbox_mcp.server.SSHBackend"):
        srv = SandboxServer()
    names = {t.name for t in srv.list_tools()}
    assert "sandbox_audit_query" not in names
    # Sanity: other tools still present
    assert "sandbox_shell_exec" in names
```

The second test constructs a fresh `SandboxServer` after `monkeypatch` has cleared the env var, so `cfg.audit.log_path` is `""` and `list_tools()` omits the audit tool.

### Step 2: Run the test to confirm it fails (tool not registered yet)

Run:
```bash
.venv/bin/python -m pytest tests/test_server.py -v
```

Expected: `test_list_tools_includes_audit_query_by_default` FAILS — `sandbox_audit_query` is not in the tool list yet.

### Step 3: Add the tool definition + conditional registration in `server.py`

In `src/sandbox_mcp/server.py`, after the existing `TOOL_DEFINITIONS` list (after line 255), append a single-element list and a helper:

```python
_AUDIT_QUERY_TOOL_DEFINITION = {
    "name": "sandbox_audit_query",
    "description": (
        "Query the audit log (read-only). Reads at most `tail` lines from "
        "the end of the file; filters apply within that tail; `start`/`end` "
        "page over the filtered results."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "tail":    {"type": "integer", "default": 5000, "minimum": 1, "maximum": 100000},
            "start":   {"type": "integer", "default": 0, "minimum": 0},
            "end":     {"type": "integer", "default": 100, "minimum": 1},
            "action":  {"type": "string"},
            "machine": {"type": "string"},
            "status":  {"type": "string"},
            "since":   {"type": "number"},
            "until":   {"type": "number"},
        },
    },
}
```

Then replace `list_tools` (lines 292–293) with:

```python
    def list_tools(self):
        tools = [ToolDef(t["name"], t["description"], t["inputSchema"]) for t in TOOL_DEFINITIONS]
        if _load_config().audit.log_path:
            t = _AUDIT_QUERY_TOOL_DEFINITION
            tools.append(ToolDef(t["name"], t["description"], t["inputSchema"]))
        return tools
```

### Step 4: Run the tests

Run:
```bash
.venv/bin/python -m pytest tests/test_server.py -v
```

Expected: both `test_list_tools_includes_audit_query_by_default` and `test_list_tools_omits_audit_query_when_log_path_empty` PASS.

### Step 5: Run lint

Run:
```bash
.venv/bin/ruff check src/sandbox_mcp/server.py tests/test_server.py
```

Expected: clean.

### Step 6: Commit

```bash
git add src/sandbox_mcp/server.py tests/test_server.py
git commit -m "feat(server): register sandbox_audit_query when log_path set"
```

---

## Task 4: Implement `_handle_sandbox_audit_query`

**Files:**
- Modify: `src/sandbox_mcp/server.py` (add handler method on `SandboxServer`)
- Test: `tests/test_audit_query.py` (add handler tests at the bottom)

### Step 1: Write the failing handler tests

Append to `tests/test_audit_query.py`:

```python
# ---------- handler integration ----------

import json
from pathlib import Path
from unittest.mock import patch

from sandbox_mcp.audit import AuditLogger
from sandbox_mcp.server import SandboxServer


def _seed_audit_log(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _build_server(monkeypatch, log_path: str | None) -> SandboxServer:
    """Build a SandboxServer with a controlled audit config.

    ``log_path`` of None means: don't touch the env var (use whatever default).
    """
    from sandbox_mcp import config as config_module

    if log_path is not None:
        monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", log_path)
    with patch("sandbox_mcp.server.DockerBackend"), patch("sandbox_mcp.server.SSHBackend"):
        return SandboxServer()


def _call_audit(server: SandboxServer, **kwargs) -> dict:
    from sandbox_mcp.server import TextContent

    result = server.call_tool("sandbox_audit_query", kwargs)
    return json.loads(result[0].text)


def test_handler_filters_by_action(monkeypatch, tmp_path):
    log = tmp_path / "audit.log"
    _seed_audit_log(log, [
        {"ts": 1.0, "machine": "dev", "action": "shell_exec", "status": "ok"},
        {"ts": 2.0, "machine": "dev", "action": "file_read", "status": "ok"},
    ])
    srv = _build_server(monkeypatch, str(log))
    data = _call_audit(srv, action="file_read")
    assert data["total"] == 1
    assert data["records"][0]["action"] == "file_read"


def test_handler_combined_filters(monkeypatch, tmp_path):
    log = tmp_path / "audit.log"
    _seed_audit_log(log, [
        {"ts": 1.0, "machine": "dev", "action": "shell_exec", "status": "ok"},
        {"ts": 2.0, "machine": "dev", "action": "shell_exec", "status": "error"},
        {"ts": 3.0, "machine": "prod", "action": "shell_exec", "status": "error"},
    ])
    srv = _build_server(monkeypatch, str(log))
    data = _call_audit(srv, machine="dev", status="error")
    assert data["total"] == 1
    assert data["records"][0]["ts"] == 2.0


def test_handler_window_pagination(monkeypatch, tmp_path):
    log = tmp_path / "audit.log"
    _seed_audit_log(log, [
        {"ts": float(i), "machine": "dev", "action": "shell_exec", "status": "ok"}
        for i in range(10)
    ])
    srv = _build_server(monkeypatch, str(log))
    page1 = _call_audit(srv, start=0, end=4)
    page2 = _call_audit(srv, start=4, end=8)
    page3 = _call_audit(srv, start=8, end=12)
    assert page1["total"] == 10
    assert page1["window"] == [0, 4]
    assert [r["ts"] for r in page1["records"]] == [0.0, 1.0, 2.0, 3.0]
    assert [r["ts"] for r in page2["records"]] == [4.0, 5.0, 6.0, 7.0]
    assert [r["ts"] for r in page3["records"]] == [8.0, 9.0]
    assert page3["window"] == [8, 10]  # clamped to total


def test_handler_end_defaults_to_start_plus_100(monkeypatch, tmp_path):
    log = tmp_path / "audit.log"
    _seed_audit_log(log, [
        {"ts": float(i), "machine": "dev", "action": "x", "status": "ok"} for i in range(5)
    ])
    srv = _build_server(monkeypatch, str(log))
    data = _call_audit(srv, start=2)  # end omitted → end = 2 + 100
    assert data["window"] == [2, 5]


def test_handler_missing_file_returns_empty(monkeypatch, tmp_path):
    log = tmp_path / "nope.log"  # does not exist
    srv = _build_server(monkeypatch, str(log))
    data = _call_audit(srv)
    assert data == {"records": [], "total": 0, "tail_size": 0, "window": [0, 0]}


def test_handler_empty_log_path_returns_error(monkeypatch):
    srv = _build_server(monkeypatch, log_path="")
    # Force the call_tool dispatch despite list_tools filtering.
    data = _call_audit(srv)
    assert "error" in data
    assert "not file-backed" in data["error"]


def test_handler_start_beyond_total(monkeypatch, tmp_path):
    log = tmp_path / "audit.log"
    _seed_audit_log(log, [{"ts": 1.0, "machine": "x", "action": "y", "status": "ok"}])
    srv = _build_server(monkeypatch, str(log))
    data = _call_audit(srv, start=10)
    assert data["records"] == []
    assert data["total"] == 1


def test_handler_tail_param_truncates(tmp_path):
    """Larger file + smaller tail → only tail lines considered."""
    log = tmp_path / "audit.log"
    _seed_audit_log(log, [
        {"ts": float(i), "machine": "x", "action": "y", "status": "ok"} for i in range(100)
    ])
    from sandbox_mcp.audit import read_tail_lines
    lines = read_tail_lines(log, 10)
    assert len(lines) == 10


def test_handler_skips_malformed_lines(monkeypatch, tmp_path):
    log = tmp_path / "audit.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"ts": 1.0, "machine": "x", "action": "good", "status": "ok"}) + "\n"
        + "garbage\n"
        + json.dumps({"ts": 2.0, "machine": "x", "action": "good", "status": "ok"}) + "\n",
        encoding="utf-8",
    )
    srv = _build_server(monkeypatch, str(log))
    data = _call_audit(srv)
    assert data["total"] == 2
```

### Step 2: Run the new tests to confirm they fail

Run:
```bash
.venv/bin/python -m pytest tests/test_audit_query.py -v -k handler
```

Expected: every test FAILS — `_handle_sandbox_audit_query` does not exist; the dispatch in `call_tool` falls through to the "Unknown tool" error.

### Step 3: Add the handler to `SandboxServer`

In `src/sandbox_mcp/server.py`, import the new helpers at the top (alongside the other `audit` imports):

```python
from sandbox_mcp.audit import (
    DEFAULT_AUDIT_LOGGER,
    AuditLogger,
    _DEFAULT_TAIL,
    apply_filters,
    parse_records,
    read_tail_lines,
    reset_default_logger,
)
```

Then add the handler method just before `def _handle_sandbox_env(self, args):` (around line 412):

```python
    # ---- audit_query handler ----

    def _handle_sandbox_audit_query(self, args):
        cfg = _load_config()
        log_path = cfg.audit.log_path
        if not log_path:
            return {"error": "audit log is not file-backed"}

        tail = int(args.get("tail", _DEFAULT_TAIL))
        start = int(args.get("start", 0))
        end = int(args.get("end", start + 100))

        path = Path(log_path).expanduser()
        raw_lines = read_tail_lines(path, tail) if path.is_file() else []
        records = list(parse_records(raw_lines))
        filtered = list(apply_filters(
            records,
            action=args.get("action"),
            machine=args.get("machine"),
            status=args.get("status"),
            since=args.get("since"),
            until=args.get("until"),
        ))
        total = len(filtered)
        window_end = min(end, total)
        window_start = min(start, total)

        return {
            "records": filtered[window_start:window_end],
            "total": total,
            "tail_size": len(raw_lines),
            "window": [window_start, window_end],
        }
```

### Step 4: Run the handler tests

Run:
```bash
.venv/bin/python -m pytest tests/test_audit_query.py -v
```

Expected: all tests PASS.

### Step 5: Run lint

Run:
```bash
.venv/bin/ruff check src/sandbox_mcp/server.py tests/test_audit_query.py
```

Expected: clean.

### Step 6: Commit

```bash
git add src/sandbox_mcp/server.py tests/test_audit_query.py
git commit -m "feat(server): implement sandbox_audit_query handler"
```

---

## Task 5: Add startup log line in `main` and `main_http`

**Files:**
- Modify: `src/sandbox_mcp/server.py` (add a log line in both entry points)

### Step 1: Add the log line in `main_http`

In `main_http` (around line 530, just before `logger.info("Starting sandbox-mcp HTTP server...")`), insert:

```python
    audit_log_path = _load_config().audit.log_path
    if audit_log_path:
        logger.info("audit: log_path=%s (query tool enabled)", audit_log_path)
    else:
        logger.warning("audit: log_path=<empty> (query tool disabled)")
```

### Step 2: Add the same log line in `main` (stdio entry point)

In `main` (around line 437, just before `server = SandboxServer()`), insert:

```python
    audit_log_path = _load_config().audit.log_path
    if audit_log_path:
        logger.info("audit: log_path=%s (query tool enabled)", audit_log_path)
    else:
        logger.warning("audit: log_path=<empty> (query tool disabled)")
```

### Step 3: Manual smoke test

Run:
```bash
.venv/bin/python -m sandbox_mcp.server --help 2>&1 | head -20
```

Expected: command help text; no Python errors.

Then:
```bash
SANDBOX_MCP_AUDIT_LOG_PATH="" .venv/bin/python -c "
from sandbox_mcp.server import main_http, main
import sys
try:
    main(['--help'])
except SystemExit:
    pass
" 2>&1 | grep -E 'audit: log_path' || echo "no log (stdio exits before logging)"
```

Expected: one of:
- `audit: log_path=<empty> (query tool disabled)`
- `no log (stdio exits before logging)` — acceptable if `--help` exits before logging.basicConfig runs

### Step 4: Run full test suite

Run:
```bash
.venv/bin/python -m pytest -x -q
```

Expected: all tests pass.

### Step 5: Run lint

Run:
```bash
.venv/bin/ruff check src/sandbox_mcp/server.py
```

Expected: clean.

### Step 6: Commit

```bash
git add src/sandbox_mcp/server.py
git commit -m "feat(server): log audit-query-tool state at startup"
```

---

## Task 6: Update README and README.zh.md

**Files:**
- Modify: `README.md`
- Modify: `README.zh.md`

### Step 1: Update the tools table in `README.md`

Find the table under `## Tools` (around line 182). Add one row after the `sandbox_env` row:

```markdown
| `sandbox_audit_query` | Read the audit log (filtered, paginated) — only when `[audit] log_path` is set |
```

### Step 2: Update the `[audit]` snippet in `README.md`

Find lines 95–96 and replace:

```toml
[audit]                 # JSON-line audit log
log_path = ""           # "" = stderr; set to a file path to append
```

with:

```toml
[audit]                 # JSON-line audit log
log_path = "~/.sandbox-mcp/audit.log"
                        # "" = stderr (sandbox_audit_query hidden); file = query tool enabled
```

### Step 3: Update the tools table in `README.zh.md`

Mirror Step 1: find the Chinese tools table and add the matching row after `sandbox_env`:

```markdown
| `sandbox_audit_query` | 读取审计日志（过滤 + 分页）—— 仅当 `[audit] log_path` 非空时暴露 |
```

### Step 4: Update the `[audit]` snippet in `README.zh.md`

Mirror Step 2: replace the Chinese audit section with the new default.

### Step 5: Commit

```bash
git add README.md README.zh.md
git commit -m "docs: document sandbox_audit_query + new audit default"
```

---

## Task 7: Final verification

**Files:** none

### Step 1: Run the full test suite

Run:
```bash
.venv/bin/python -m pytest -q
```

Expected: all tests PASS (existing + new). Capture the count for the commit message.

### Step 2: Run lint

Run:
```bash
.venv/bin/ruff check .
```

Expected: clean.

### Step 3: Run mypy (project uses mypy per pyproject.toml)

Run:
```bash
.venv/bin/mypy src/sandbox_mcp/audit.py src/sandbox_mcp/server.py src/sandbox_mcp/config.py
```

Expected: no new errors (existing baseline may have a few that we don't touch).

### Step 4: Commit any cleanup

If lint/mypy surfaced fixes:
```bash
git add -u
git commit -m "chore: lint + typecheck cleanup"
```

If everything was clean, no commit needed.

### Step 5: Diff summary

Run:
```bash
git log --oneline main..HEAD
git diff --stat main..HEAD
```

Expected: 7 commits (Tasks 1–6 + optional 7), touching roughly:
- `src/sandbox_mcp/audit.py` (+50 lines)
- `src/sandbox_mcp/config.py` (1 line changed)
- `src/sandbox_mcp/server.py` (+60 lines)
- `config/config.example.toml` (~5 lines)
- `tests/test_audit_query.py` (new, ~250 lines)
- `tests/test_server.py` (~10 lines added)
- `README.md`, `README.zh.md` (3 lines each)

---

## Self-Review Notes

**Spec coverage:**

| Spec section | Implemented in |
|--------------|---------------|
| Default log_path | Task 2 |
| Conditional tool exposure | Tasks 3 + 5 (startup log) |
| Tool parameters & constraints | Task 4 (handler validation) + Task 1 (`read_tail_lines` enforces bounds) |
| Return shape | Task 4 |
| Behaviour table | Tasks 1 + 4 (each row has a corresponding test or branch) |
| Security (read-only, path internal) | Task 4 (no write methods; path comes from config) |
| Performance (deque-based) | Task 1 (`read_tail_lines` uses `deque(f, maxlen=n)`) |
| Test plan | Tasks 1, 3, 4 |
| Migration note | Task 6 (README) |

**Type consistency:** `_AUDIT_QUERY_TOOL_DEFINITION` matches the docstring table; `_DEFAULT_TAIL` is used in both handler and tool schema default. `_MAX_TAIL` is enforced in `read_tail_lines` and reflected in the JSON schema `maximum`.

**Placeholder scan:** No "TBD" / "TODO" in steps. All code is concrete.