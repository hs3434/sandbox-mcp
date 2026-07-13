# Self-Audit Tool — Design Spec

Date: 2026-07-13
Status: Approved

## Problem

sandbox-mcp records every tool call the agent makes into an audit log
on the MCP server host (`[audit] log_path` in `config.toml`). Today
the agent has no MCP-accessible way to read this log:

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
must not be able to modify, delete, or otherwise tamper with the log.

## Non-Goals

- Agent cannot modify / delete records
- Agent cannot change logging behaviour (level, sinks)
- No real-time push / streaming subscriptions
- No access to records older than the `tail` window
- No cross-server log aggregation — each MCP server queries its own log

## Design

### Storage: SQLite (stdlib `sqlite3`, no new dependency)

The audit log is stored in a single SQLite database file. SQLite
gives us:

- **Indexed queries** on `ts`, `action`, `machine`, `status` —
  O(log n) lookup instead of full-file scan
- **No file-size growth problem** — append-only, single file
- **No rotation needed** — file is naturally bounded by write rate,
  not by fragmentation
- **Standard tooling** — operators can `sqlite3 ~/.sandbox-mcp/audit.db
  "SELECT * FROM audit WHERE..."` directly
- **ACID** — no risk of partial-write corruption
- **Zero new dependency** — `sqlite3` is in Python's standard library

`AuditLogger` writes to the DB. Schema:

```sql
CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,
    machine      TEXT,
    action       TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    duration_ms  INTEGER,
    details      TEXT    -- JSON blob
);
CREATE INDEX IF NOT EXISTS idx_audit_ts      ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action  ON audit(action);
CREATE INDEX IF NOT EXISTS idx_audit_machine ON audit(machine);
CREATE INDEX IF NOT EXISTS idx_audit_status  ON audit(status);
```

`details` is stored as a JSON string (Python `json.dumps` on write,
`json.loads` on read). This preserves arbitrary key/value pairs
without forcing schema changes when new fields are added.

### Default log path

`AuditConfig.log_path` default changes from `""` (stderr) to
`"~/.sandbox-mcp/audit.db"`. Rationale: persistent storage is the
norm; stderr-only mode is the exception (only useful when an external
log collector is attached). A file default also makes
`sandbox_audit_query` available out of the box — no extra config
required.

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
| `tail`    | int            | 5000    | Consider at most this many recent records  |
| `start`   | int            | 0       | Offset in filtered results (>= 0)          |
| `end`     | int            | 100     | End offset in filtered results (exclusive) |
| `action`  | str \| None    | None    | Filter by `action` column                  |
| `machine` | str \| None    | None    | Filter by `machine` column                 |
| `status`  | str \| None    | None    | Filter by `status` column                  |
| `since`   | float \| None  | None    | Unix ts; records with `ts >= since`        |
| `until`   | float \| None  | None    | Unix ts; records with `ts < until`         |

#### Constraints

- `tail` must be `0 < tail <= 100000` (hard cap; `ValueError` otherwise)
- `start >= 0`, `end > start`
- `end` defaults to `start + 100` when omitted
- All filters are applied **inside** the tail subquery — older
  records are not reachable through this tool

#### Return shape

```json
{
  "records": [
    {"ts": 1234567890.0, "machine": "dev", "action": "shell_exec",
     "status": "ok", "duration_ms": 42, "details": {"command": "ls"}}
  ],
  "total": 123,
  "tail_size": 5000,
  "window": [0, 100]
}
```

- `records` — filtered window `[start, end)`, ordered chronologically
- `total` — total filtered count within the tail subquery
- `tail_size` — actual rows considered (may be < requested `tail`
  if the table is smaller)
- `window` — `[start, min(end, total)]`, the offset range actually
  returned

### Behaviour table

| Condition                            | Behaviour                                |
|--------------------------------------|------------------------------------------|
| `log_path` empty at startup          | Tool not exposed (filtered from `list_tools()`) |
| DB file missing                      | `{records: [], total: 0, tail_size: 0}`  |
| Empty table                          | Same as missing                          |
| `start >= total`                     | Empty `records`; `total` still reported  |
| `end > total`                        | `end` clamped to `total`                 |
| `tail <= 0` or `tail > 100000`       | `ValueError`                             |
| `start < 0`, `end < 1`, `end <= start` | `ValueError`                           |

### Query implementation

One SQL query, with the `tail` cap applied as an inner subquery so
pagination and filtering happen on the bounded subset:

```sql
SELECT * FROM (
    SELECT * FROM audit ORDER BY id DESC LIMIT :tail
) sub
WHERE [filters]
ORDER BY id ASC
LIMIT :limit OFFSET :offset
```

A separate `SELECT COUNT(*) FROM (subquery)` produces the `total`.
Filters are built as `?` placeholders, never string-interpolated:

```python
where_clauses, params = [], []
if action:    where_clauses.append("action = ?");  params.append(action)
if machine:   where_clauses.append("machine = ?"); params.append(machine)
if status:    where_clauses.append("status = ?");  params.append(status)
if since:     where_clauses.append("ts >= ?");     params.append(since)
if until:     where_clauses.append("ts < ?");      params.append(until)
where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
```

Inner `ORDER BY id DESC` plus outer `ORDER BY id ASC` restores
chronological order. Indexes on `id` (primary key) and the filter
columns make the inner subquery cheap.

### Tool definition (MCP `tools/list`)

```python
Tool(
    name="sandbox_audit_query",
    description=(
        "Query the audit log (read-only). Considers at most `tail` recent "
        "records; filters apply within that window; `start`/`end` page over "
        "the filtered results."
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

- Read-only by design — handler only runs `SELECT`, no `INSERT` /
  `UPDATE` / `DELETE` exposed to the agent
- Path is server-internal; agent passes filters, not paths
- All filter values are bound parameters (`:tail`, `?`), no string
  interpolation — eliminates SQL injection
- DB connection is opened read-only at the connection level
  (`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`) once the
  schema is known to exist

### Performance

- Indexed columns (`ts`, `action`, `machine`, `status`) keep filter
  lookups at O(log n) regardless of total table size
- Inner subquery `ORDER BY id DESC LIMIT N` is fast — `id` is the
  primary key
- Pagination via `LIMIT/OFFSET` is constant-time on the filtered set
- No caching — each query reads fresh, so a record emitted by the
  just-completed action is immediately visible
- No file-size concern — SQLite handles arbitrarily large single
  files efficiently

## Test Plan

New file `tests/test_audit_query.py`:

- Single-field filters: `action`, `machine`, `status`, `since`, `until`
- Combined filters (action + machine + status + since)
- Window boundary: empty page (start >= total), single page, multi-page
- `end > total` clamps to total
- `tail` cap: 0 → ValueError, 100001 → ValueError, 100000 → OK
- `start`/`end` validation: negative, equal, inverted → ValueError
- Missing DB file → empty result, no error
- Empty DB → empty result, no error
- `list_tools()` omits the tool when `log_path` is empty
- Records emitted between two queries are visible to the second
  (no stale caching)
- Handler is read-only (no `INSERT`/`UPDATE`/`DELETE` exposed)
- Filter values are parameterized (no SQL injection possible)

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