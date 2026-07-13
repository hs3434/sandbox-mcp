# Self-Audit Tool — Design Spec

Date: 2026-07-13
Status: Draft (pending user review)

## Problem

sandbox-mcp records every tool call the agent makes into a JSON-line
audit log on the MCP server host (`[audit] log_path` in
`config.toml`). Today the agent has no MCP-accessible way to read this
log:

- `sandbox_file_*` only sees paths inside sandboxes (containers/SSH
  hosts). The audit log lives on the MCP server host, outside any
  sandbox.
- The MCP server exposes no host-side file-reading tool.

Without self-audit access, the agent cannot:

- Review what it just did (post-action verification)
- Diagnose failures after the fact
- Reason about its own history ("which machines have I touched?",
  "what commands failed last session?")
- Catch accidental destructive actions before they compound

## Goal

Expose a single read-only MCP tool, `sandbox_audit_query`, that lets
the agent query the audit log filtered by various criteria. The agent
must not be able to modify, delete, rotate, or otherwise tamper with
the log.

## Non-Goals

- Agent cannot modify / rotate / clear the log
- Agent cannot change logging behaviour (level, sinks)
- No real-time push / streaming subscriptions
- No access to records older than the `tail` window
- No cross-server log aggregation — each MCP server queries its own log

## Design

### Default log path

`AuditConfig.log_path` default changes from `""` (stderr) to
`"~/.sandbox-mcp/audit.db"`. Rationale: persistent storage is the
norm; stderr-only mode is the exception (only useful when an external
log collector is attached). A file default also makes
`sandbox_audit_query` available out of the box — no extra config
required.

`config.example.toml` `[audit]` section updated to comment out the
default with the path spelled out.

### Conditional tool exposure

`list_tools()` reads `cfg.audit.log_path` and omits
`sandbox_audit_query` from the returned tool list when the path is
empty. The agent never sees a tool it cannot use; there is no error
path for "not file-backed".

- Server logs a one-line startup message:
  - `audit: log_path=~/.sandbox-mcp/audit.db (query tool enabled)`
    or
  - `audit: log_path=<empty> (query tool disabled)`
- The audit config is loaded once at server startup (consistent with
  the rest of the config pipeline — there is no hot-reload for
  audit). The tool's presence/absence therefore reflects the value
  at startup, not live edits.

### Tool: `sandbox_audit_query`

Single parameterized tool. Path comes from server config; the agent
never sees the raw path.

#### Parameters

| Name      | Type           | Default | Description                                |
|-----------|----------------|---------|--------------------------------------------|
| `tail`    | int            | 5000    | Read at most this many lines from file end |
| `start`   | int            | 0       | Offset in filtered results (>= 0)          |
| `end`     | int            | 100     | End offset in filtered results (exclusive) |
| `action`  | str \| None    | None    | Filter by `action` field                   |
| `machine` | str \| None    | None    | Filter by `machine` field                  |
| `status`  | str \| None    | None    | Filter by `status` field                   |
| `since`   | float \| None  | None    | Unix ts; records with `ts >= since`        |
| `until`   | float \| None  | None    | Unix ts; records with `ts < until`         |

#### Constraints

- `tail` must be `0 < tail <= 100000` (hard cap; `ValueError` otherwise)
- `start >= 0`, `end > start`
- `end` defaults to `start + 100` when omitted (so the default
  page is always 100 records wide regardless of `start`)
- All filters are applied **inside** the tail slice — older records
  are not reachable through this tool

#### Return shape

```json
{
  "records": [
    {"ts": 1234567890.0, "machine": "dev", "action": "shell_exec",
     "status": "ok", "duration_ms": 42, "details": {...}}
  ],
  "total": 123,
  "tail_size": 5000,
  "window": [0, 100]
}
```

- `records` — filtered window `[start, end)`, parsed from JSON
- `total` — total filtered count within the tail
- `tail_size` — actual lines read from disk (may be < requested `tail`
  if the file is shorter)
- `window` — `[start, min(end, total)]`, the offset range actually
  returned (useful when `end > total` clamps)

### Behaviour table

| Condition                            | Behaviour                                |
|--------------------------------------|------------------------------------------|
| `log_path` empty at startup          | Tool not exposed (filtered from `list_tools()`) |
| File missing                         | `{records: [], total: 0, tail_size: 0}`  |
| File empty                           | Same as missing                          |
| `start >= total`                     | Empty `records`; `total` still reported  |
| `end > total`                        | `end` clamped to `total`                 |
| Malformed JSON line                  | Skip line; log warning to server stderr  |
| `tail <= 0` or `tail > 100000`       | `ValueError`                             |
| `start < 0`, `end < 0`, `end <= start` | `ValueError`                           |

### Implementation outline

In `src/sandbox_mcp/audit.py`:

```python
from collections import deque
from pathlib import Path

MAX_TAIL = 100_000
DEFAULT_TAIL = 5_000


def read_tail_lines(path: Path, n: int) -> list[str]:
    if n <= 0 or n > MAX_TAIL:
        raise ValueError(f"tail must be in (0, {MAX_TAIL}], got {n}")
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return list(deque(f, maxlen=n))


def parse_records(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            logger.warning("audit: skipping malformed line: %r", line[:120])


def apply_filters(records, *, action, machine, status, since, until):
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

In `src/sandbox_mcp/server.py`, register the tool:

```python
def call_audit_query(self, args):
    cfg = _load_config()
    log_path = cfg.audit.log_path
    # Empty log_path is filtered out of list_tools(), so this branch is
    # defensive: only reachable if config changes mid-session.
    if not log_path:
        return [TextContent(json.dumps({
            "error": "audit log is not file-backed",
        }))]

    tail = int(args.get("tail", DEFAULT_TAIL))
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

    return [TextContent(json.dumps({
        "records": filtered[window_start:window_end],
        "total": total,
        "tail_size": len(raw_lines),
        "window": [window_start, window_end],
    }))]
```

In `list_tools()`, append the audit tool only when `log_path` is set:

```python
def list_tools(self):
    tools = [...existing 7 tools...]
    if _load_config().audit.log_path:
        tools.append(_AUDIT_QUERY_TOOL)
    return tools
```

Add a startup log line indicating audit state:

```python
log_path = _load_config().audit.log_path
if log_path:
    logger.info("audit: log_path=%s (query tool enabled)", log_path)
else:
    logger.warning("audit: log_path=<empty> (query tool disabled)")
```

### Tool definition (MCP `tools/list`)

```python
Tool(
    name="sandbox_audit_query",
    description=(
        "Query the audit log (read-only). Reads at most `tail` lines from "
        "the end of the file; filters apply within that tail; `start`/`end` "
        "page over the filtered results."
    ),
    inputSchema={
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
)
```

### Security

- Read-only by design — no write / delete / rotate method exists
- Path is server-internal; agent passes filters, not paths
- `errors="replace"` on open prevents binary content from crashing
  the tool
- Skipped lines logged to server stderr only (not surfaced to agent,
  so log content does not leak via warnings)

### Performance

- `deque(f, maxlen=tail)` reads the file once into memory
- Bounded by `tail <= 100000`; at ~500 bytes/record ≈ 50MB max
- Subsequent filtering is O(N) in tail size, in-memory only
- No caching — each query reads fresh, so a record emitted by the
  just-completed action is immediately visible to the next query
- Single read per query; no chunked reverse scan needed

## Test Plan

New file `tests/test_audit_query.py`:

- Single-field filters: `action`, `machine`, `status`, `since`, `until`
- Combined filters (action + machine + since)
- Window boundary: empty page (start >= total), single page, multi-page
- `end > total` clamps to total
- `tail` cap: 0 → ValueError, 100001 → ValueError, 100000 → OK
- `start`/`end` validation: negative, equal, inverted → ValueError
- Missing file → empty result, no error
- Empty file → empty result, no error
- Malformed lines skipped, stderr warning emitted
- `list_tools()` omits the tool when `log_path` is empty (set
  `SANDBOX_MCP_AUDIT_LOG_PATH=""` and verify tool absent from list)
- Records emitted between two queries are visible to the second
  (no stale caching)

## Migration / Compatibility

- Purely additive at the API level: one new tool, no change to
  existing tools
- **Behavioural change**: `[audit] log_path` default moves from `""`
  to `"~/.sandbox-mcp/audit.db"`. Existing deployments that relied
  on stderr logging will now write to a SQLite file by default. To
  restore the old behaviour, set `log_path = ""` (or
  `SANDBOX_MCP_AUDIT_LOG_PATH=""`).
- No env var rename; `SANDBOX_MCP_AUDIT_LOG_PATH` semantics unchanged

## Open Questions

None.