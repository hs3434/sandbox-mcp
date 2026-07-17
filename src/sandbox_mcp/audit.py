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

"""Audit logger for sandbox-mcp.

Each call to :meth:`AuditLogger.record` appends a row to the configured
sink. File-operation contents are never stored in full — only a short
SHA-256 fingerprint — so the audit database is safe to ship to remote
log collectors without leaking secrets.

The default sink is a SQLite database at
``~/.sandbox-mcp/audit.db`` (configurable via the ``[audit] log_path``
setting in ``config.toml`` or the ``SANDBOX_MCP_AUDIT_LOG_PATH`` env
var).  Set ``log_path`` to an empty string to keep the legacy
stderr/file behaviour.  A path ending in ``.db`` selects SQLite mode;
any other non-empty path selects append-mode JSON-line (legacy).

The companion read-side helper :func:`query_audit` powers the
``audit_query`` MCP tool: it runs a single SQL query against
the database with optional filters and pagination.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import IO, Any

from sandbox_mcp.config import load as _load_config

_CONTENT_KEYS = {"content"}
_BINARY_HASH_LEN = 16

# Public bounds for ``audit_query``'s ``tail`` parameter.
DEFAULT_TAIL = 5_000
MAX_TAIL = 100_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,
    machine      TEXT,
    action       TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    duration_ms  INTEGER,
    details      TEXT
)
"""
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_audit_ts      ON audit(ts)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action  ON audit(action)",
    "CREATE INDEX IF NOT EXISTS idx_audit_machine ON audit(machine)",
    "CREATE INDEX IF NOT EXISTS idx_audit_status  ON audit(status)",
)

logger = logging.getLogger(__name__)


def _open_json_sink(log_path: str) -> IO[str]:
    """Return an append-mode text stream for JSON-line records.

    Empty / unset ``log_path`` ⇒ stderr.  Parent dirs are created
    on demand.
    """
    if not log_path:
        return sys.stderr
    p = Path(log_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p.open("a", encoding="utf-8")


def _open_db(log_path: str) -> sqlite3.Connection:
    """Open (or create) the audit SQLite database and ensure schema."""
    p = Path(log_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(_SCHEMA)
    for stmt in _INDEXES:
        conn.execute(stmt)
    conn.commit()
    return conn


class AuditLogger:
    """Append audit records to a sink (default: configured SQLite path).

    The sink is dispatched by type:

    - ``None`` → load ``[audit] log_path`` from config (default
      ``~/.sandbox-mcp/audit.db`` → SQLite)
    - path ending in ``.db`` → SQLite
    - other non-empty string path → append-mode JSON-line file
    - empty string ``""`` → stderr
    - any IO stream → JSON-line to that stream (legacy)
    """

    def __init__(self, sink: IO[str] | str | None = None) -> None:
        self._db: sqlite3.Connection | None = None
        self._sink: IO[str] | None = None
        if sink is None:
            sink = _load_config().audit.log_path
        if isinstance(sink, str):
            if sink == "":
                self._sink = sys.stderr
            elif sink.endswith(".db"):
                self._db = _open_db(sink)
            else:
                self._sink = _open_json_sink(sink)
        else:
            self._sink = sink
        self._closed = False

    def record(
        self,
        *,
        machine: str | None,
        action: str,
        status: str = "ok",
        duration_ms: int | None = None,
        **details: Any,
    ) -> None:
        """Append one audit record.

        ``machine`` may be ``None`` for actions that don't apply to a
        specific machine (e.g. ``env(action="help")``).
        ``status`` is a short string like ``"ok"`` / ``"error"``.
        ``duration_ms`` is the wall-clock duration of the action.
        Any extra keyword arguments are recorded under ``details``;
        keys named in :data:`_CONTENT_KEYS` are hashed and replaced
        with ``content_sha256`` so raw content never leaks to logs.
        """
        if self._closed:
            return
        sanitized: dict[str, Any] = {}
        for key, value in details.items():
            if key in _CONTENT_KEYS and isinstance(value, str):
                digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
                sanitized["content_sha256"] = digest[:_BINARY_HASH_LEN]
                sanitized[f"{key}_len"] = len(value)
            else:
                sanitized[key] = value
        ts = time.time()
        details_json = json.dumps(sanitized, ensure_ascii=False, default=str) if sanitized else None
        try:
            if self._db is not None:
                self._db.execute(
                    "INSERT INTO audit (ts, machine, action, status, duration_ms, details) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ts, machine, action, status, duration_ms, details_json),
                )
                self._db.commit()
            elif self._sink is not None:
                entry: dict[str, Any] = {
                    "ts": ts,
                    "machine": machine,
                    "action": action,
                    "status": status,
                    "duration_ms": duration_ms,
                }
                if sanitized:
                    entry["details"] = sanitized
                line = json.dumps(entry, ensure_ascii=False, default=str)
                self._sink.write(line + "\n")
                self._sink.flush()
        except Exception:
            # Never let audit logging break the actual tool call.
            logger.exception("audit: record failed")

    def close(self) -> None:
        self._closed = True
        if self._db is not None:
            with contextlib.suppress(Exception):
                self._db.close()
            self._db = None


DEFAULT_AUDIT_LOGGER = AuditLogger()


def reset_default_logger(sink: IO[str] | str | None = None) -> AuditLogger:
    """Rebuild :data:`DEFAULT_AUDIT_LOGGER` (used at server startup to pick
    up ``[audit] log_path`` after env or config changes).
    """
    global DEFAULT_AUDIT_LOGGER
    with contextlib.suppress(Exception):
        DEFAULT_AUDIT_LOGGER.close()
    DEFAULT_AUDIT_LOGGER = AuditLogger(sink)
    return DEFAULT_AUDIT_LOGGER


def query_audit(
    db_path: Path,
    *,
    tail: int = DEFAULT_TAIL,
    start: int = 0,
    end: int | None = None,
    action: str | None = None,
    machine: str | None = None,
    status: str | None = None,
    since: float | None = None,
    until: float | None = None,
) -> dict[str, Any]:
    """Read records from the audit DB with optional filters and pagination.

    Returns a dict ``{records, total, tail_size}``:

    - ``records`` — list of dicts in chronological order, the
      requested window ``[start, end)``
    - ``total`` — total filtered count within the tail subquery
    - ``tail_size`` — actual rows the inner subquery scanned (the
      table size, capped at ``tail``)

    Filters are applied with bound parameters (no SQL injection).
    Missing or empty database returns an empty result, not an error.
    """
    if not (0 < tail <= MAX_TAIL):
        raise ValueError(f"tail must be in (0, {MAX_TAIL}], got {tail}")
    if start < 0:
        raise ValueError(f"start must be >= 0, got {start}")
    if end is None:
        end = start + 100
    if end < 1:
        raise ValueError(f"end must be >= 1, got {end}")
    if end <= start:
        raise ValueError(f"end ({end}) must be > start ({start})")

    where_clauses: list[str] = []
    params: list[Any] = []
    if action is not None:
        where_clauses.append("action = ?")
        params.append(action)
    if machine is not None:
        where_clauses.append("machine = ?")
        params.append(machine)
    if status is not None:
        where_clauses.append("status = ?")
        params.append(status)
    if since is not None:
        where_clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        where_clauses.append("ts < ?")
        params.append(until)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    if not db_path.is_file():
        return {"records": [], "total": 0, "tail_size": 0}

    with sqlite3.connect(db_path) as conn:
        tail_size = conn.execute(
            "SELECT COUNT(*) FROM (SELECT * FROM audit ORDER BY id DESC LIMIT ?)",
            (tail,),
        ).fetchone()[0]
        total = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT * FROM audit ORDER BY id DESC LIMIT ?) {where_sql}",
            (tail, *params),
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT ts, machine, action, status, duration_ms, details "
            f"FROM (SELECT * FROM audit ORDER BY id DESC LIMIT ?) {where_sql} "
            f"ORDER BY id ASC LIMIT ? OFFSET ?",
            (tail, *params, end - start, start),
        ).fetchall()

    records: list[dict[str, Any]] = []
    for ts, mch, act, sts, dur, det in rows:
        rec: dict[str, Any] = {
            "ts": ts,
            "machine": mch,
            "action": act,
            "status": sts,
            "duration_ms": dur,
        }
        if det:
            try:
                rec["details"] = json.loads(det)
            except json.JSONDecodeError:
                logger.warning("audit: failed to parse details JSON: %r", det[:120])
        records.append(rec)

    return {"records": records, "total": total, "tail_size": tail_size}
