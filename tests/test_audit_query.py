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
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from sandbox_mcp.audit import (
    DEFAULT_TAIL,
    MAX_TAIL,
    AuditLogger,
    query_audit,
)


@pytest.fixture(autouse=True)
def _disable_default_machine(monkeypatch):
    """Unit tests run in lazy mode — see test_server.py for rationale."""
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "false")


# ---------- AuditLogger SQLite write path ----------


def test_audit_logger_writes_to_sqlite(tmp_path):
    db = tmp_path / "audit.db"
    log = AuditLogger(sink=str(db))
    log.record(machine="dev", action="shell_exec", status="ok", duration_ms=42, command="ls")
    log.close()

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT ts, machine, action, status, duration_ms, details FROM audit"
        ).fetchall()
    assert len(rows) == 1
    _ts, mch, act, sts, dur, det = rows[0]
    assert mch == "dev"
    assert act == "shell_exec"
    assert sts == "ok"
    assert dur == 42
    assert json.loads(det) == {"command": "ls"}


def test_audit_logger_creates_schema_and_indexes(tmp_path):
    db = tmp_path / "audit.db"
    AuditLogger(sink=str(db)).close()

    with sqlite3.connect(db) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_audit_%'"
            ).fetchall()
        }
    assert "audit" in tables
    assert {"idx_audit_ts", "idx_audit_action", "idx_audit_machine", "idx_audit_status"} <= indexes


def test_audit_logger_hashes_content(tmp_path):
    db = tmp_path / "audit.db"
    log = AuditLogger(sink=str(db))
    log.record(machine="dev", action="file_write", path="/tmp/x.py", content="print('hello')\n")
    log.close()

    with sqlite3.connect(db) as conn:
        det = conn.execute("SELECT details FROM audit").fetchone()[0]
    parsed = json.loads(det)
    assert "content" not in parsed
    assert "content_sha256" in parsed
    assert len(parsed["content_sha256"]) == 16
    assert parsed["content_len"] == len("print('hello')\n")


def test_audit_logger_allows_null_machine(tmp_path):
    db = tmp_path / "audit.db"
    AuditLogger(sink=str(db)).record(machine=None, action="help", status="ok")
    AuditLogger(sink=str(db)).close()

    with sqlite3.connect(db) as conn:
        mch = conn.execute("SELECT machine FROM audit").fetchone()[0]
    assert mch is None


def test_audit_logger_creates_parent_dirs(tmp_path):
    db = tmp_path / "nested" / "deeper" / "audit.db"
    AuditLogger(sink=str(db)).close()
    assert db.is_file()


# ---------- query_audit pure function ----------


def _r(ts, action="shell_exec", machine="dev", status="ok", **details):
    return {"ts": ts, "action": action, "machine": machine, "status": status, **details}


def _seed(db: Path, records: list[dict]) -> None:
    """Insert records directly into the audit table (creates schema if needed)."""
    AuditLogger(sink=str(db)).close()  # ensure schema + indexes exist
    with sqlite3.connect(db) as conn:
        for r in records:
            conn.execute(
                "INSERT INTO audit (ts, machine, action, status, duration_ms, details) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    r["ts"],
                    r.get("machine"),
                    r["action"],
                    r["status"],
                    r.get("duration_ms"),
                    json.dumps(r.get("details")) if "details" in r else None,
                ),
            )
        conn.commit()


def test_query_returns_all_when_no_filter(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(1.0), _r(2.0), _r(3.0)])
    out = query_audit(db)
    assert out["total"] == 3
    assert [r["ts"] for r in out["records"]] == [1.0, 2.0, 3.0]


def test_query_filter_action(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(1.0, action="shell_exec"), _r(2.0, action="file_read")])
    out = query_audit(db, action="file_read")
    assert out["total"] == 1
    assert out["records"][0]["action"] == "file_read"


def test_query_filter_machine(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(1.0, machine="a"), _r(2.0, machine="b")])
    out = query_audit(db, machine="b")
    assert out["total"] == 1
    assert out["records"][0]["machine"] == "b"


def test_query_filter_status(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(1.0, status="ok"), _r(2.0, status="error")])
    out = query_audit(db, status="error")
    assert out["total"] == 1
    assert out["records"][0]["status"] == "error"


def test_query_filter_since_inclusive(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(1.0), _r(2.0), _r(3.0)])
    out = query_audit(db, since=2.0)
    assert [r["ts"] for r in out["records"]] == [2.0, 3.0]


def test_query_filter_until_exclusive(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(1.0), _r(2.0), _r(3.0)])
    out = query_audit(db, until=2.0)
    assert [r["ts"] for r in out["records"]] == [1.0]


def test_query_combined_filters(tmp_path):
    db = tmp_path / "a.db"
    _seed(
        db,
        [
            _r(1.0, action="shell_exec", machine="dev", status="ok"),
            _r(2.0, action="shell_exec", machine="dev", status="error"),
            _r(3.0, action="shell_exec", machine="prod", status="error"),
            _r(4.0, action="file_read", machine="dev", status="error"),
        ],
    )
    out = query_audit(db, action="shell_exec", machine="dev", status="error", since=1.5)
    assert [r["ts"] for r in out["records"]] == [2.0]


def test_query_pagination(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(float(i)) for i in range(10)])
    page1 = query_audit(db, start=0, end=4)
    page2 = query_audit(db, start=4, end=8)
    page3 = query_audit(db, start=8, end=12)
    assert page1["total"] == 10
    assert [r["ts"] for r in page1["records"]] == [0.0, 1.0, 2.0, 3.0]
    assert [r["ts"] for r in page2["records"]] == [4.0, 5.0, 6.0, 7.0]
    assert [r["ts"] for r in page3["records"]] == [8.0, 9.0]


def test_query_end_defaults_to_start_plus_100(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(float(i)) for i in range(150)])
    out = query_audit(db, start=20)  # end omitted → end = 20 + 100
    assert [r["ts"] for r in out["records"]] == [float(i) for i in range(20, 120)]


def test_query_end_clamps_to_total(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(float(i)) for i in range(5)])
    out = query_audit(db, start=0, end=100)
    assert out["total"] == 5
    assert len(out["records"]) == 5


def test_query_start_beyond_total(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(1.0)])
    out = query_audit(db, start=10)
    assert out["records"] == []
    assert out["total"] == 1


def test_query_tail_bounds_inner_subquery(tmp_path):
    """``tail`` limits how many recent records are considered (inner LIMIT)."""
    db = tmp_path / "a.db"
    _seed(db, [_r(float(i)) for i in range(100)])
    out = query_audit(db, tail=10)
    assert out["tail_size"] == 10
    assert out["total"] == 10
    assert [r["ts"] for r in out["records"]] == [float(i) for i in range(90, 100)]


def test_query_missing_file_returns_empty(tmp_path):
    db = tmp_path / "nope.db"
    out = query_audit(db)
    assert out == {"records": [], "total": 0, "tail_size": 0}


def test_query_empty_db_returns_empty(tmp_path):
    db = tmp_path / "a.db"
    AuditLogger(sink=str(db)).close()  # creates empty schema
    out = query_audit(db)
    assert out == {"records": [], "total": 0, "tail_size": 0}


def test_query_rejects_zero_tail(tmp_path):
    db = tmp_path / "a.db"
    AuditLogger(sink=str(db)).close()
    with pytest.raises(ValueError, match=r"tail must be in"):
        query_audit(db, tail=0)


def test_query_rejects_negative_tail(tmp_path):
    db = tmp_path / "a.db"
    AuditLogger(sink=str(db)).close()
    with pytest.raises(ValueError, match=r"tail must be in"):
        query_audit(db, tail=-1)


def test_query_rejects_over_cap_tail(tmp_path):
    db = tmp_path / "a.db"
    AuditLogger(sink=str(db)).close()
    with pytest.raises(ValueError, match=r"tail must be in"):
        query_audit(db, tail=MAX_TAIL + 1)


def test_query_accepts_max_tail(tmp_path):
    db = tmp_path / "a.db"
    AuditLogger(sink=str(db)).close()
    out = query_audit(db, tail=MAX_TAIL)
    assert out["total"] == 0  # empty table


def test_query_rejects_negative_start(tmp_path):
    db = tmp_path / "a.db"
    AuditLogger(sink=str(db)).close()
    with pytest.raises(ValueError, match=r"start must be"):
        query_audit(db, start=-1)


def test_query_rejects_non_positive_end(tmp_path):
    db = tmp_path / "a.db"
    AuditLogger(sink=str(db)).close()
    with pytest.raises(ValueError, match=r"end must be"):
        query_audit(db, end=0)


def test_query_rejects_inverted_window(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(float(i)) for i in range(5)])
    with pytest.raises(ValueError, match=r"end .* must be > start"):
        query_audit(db, start=4, end=2)


def test_query_rejects_equal_window(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, [_r(float(i)) for i in range(5)])
    with pytest.raises(ValueError, match=r"end .* must be > start"):
        query_audit(db, start=3, end=3)


def test_query_no_caching_sees_new_records(tmp_path):
    """Records inserted between two queries must be visible to the second."""
    db = tmp_path / "a.db"
    AuditLogger(sink=str(db)).close()

    out1 = query_audit(db)
    assert out1["total"] == 0

    AuditLogger(sink=str(db)).record(machine="x", action="between")

    out2 = query_audit(db)
    assert out2["total"] == 1
    assert out2["records"][0]["action"] == "between"


def test_query_preserves_details_dict(tmp_path):
    db = tmp_path / "a.db"
    log = AuditLogger(sink=str(db))
    log.record(machine="dev", action="shell_exec", status="ok", command="ls -la", timeout=30)
    log.close()

    out = query_audit(db)
    assert out["total"] == 1
    assert out["records"][0]["details"] == {"command": "ls -la", "timeout": 30}


# ---------- constants ----------


def test_default_tail_is_5000():
    assert DEFAULT_TAIL == 5000


def test_max_tail_is_100000():
    assert MAX_TAIL == 100_000


# ---------- handler integration ----------


def _build_server(monkeypatch, log_path: str | None):
    """Build a SandboxServer with a controlled audit config."""
    from sandbox_mcp.server import SandboxServer as _Srv

    if log_path is not None:
        monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", log_path)
    with patch("sandbox_mcp.server.DockerBackend"), patch("sandbox_mcp.server.SSHBackend"):
        return _Srv()


def _call_audit(server, **kwargs):
    result = server.call_tool("audit_query", kwargs)
    return json.loads(result[0].text)


def test_handler_filters_by_action(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    _seed(
        db,
        [
            _r(1.0, machine="dev", action="shell_exec", status="ok"),
            _r(2.0, machine="dev", action="file_read", status="ok"),
        ],
    )
    srv = _build_server(monkeypatch, str(db))
    data = _call_audit(srv, action="file_read")
    assert data["total"] == 1
    assert data["records"][0]["action"] == "file_read"


def test_handler_combined_filters(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    _seed(
        db,
        [
            _r(1.0, machine="dev", action="shell_exec", status="ok"),
            _r(2.0, machine="dev", action="shell_exec", status="error"),
            _r(3.0, machine="prod", action="shell_exec", status="error"),
        ],
    )
    srv = _build_server(monkeypatch, str(db))
    data = _call_audit(srv, machine="dev", status="error")
    assert data["total"] == 1
    assert data["records"][0]["ts"] == 2.0


def test_handler_window_pagination(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    _seed(db, [_r(float(i), machine="dev", action="shell_exec", status="ok") for i in range(10)])
    srv = _build_server(monkeypatch, str(db))
    page1 = _call_audit(srv, start=0, end=4)
    page2 = _call_audit(srv, start=4, end=8)
    page3 = _call_audit(srv, start=8, end=12)
    assert page1["total"] == 10
    assert [r["ts"] for r in page1["records"]] == [0.0, 1.0, 2.0, 3.0]
    assert [r["ts"] for r in page2["records"]] == [4.0, 5.0, 6.0, 7.0]
    assert [r["ts"] for r in page3["records"]] == [8.0, 9.0]


def test_handler_end_defaults_to_start_plus_100(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    _seed(db, [_r(float(i), machine="dev", action="x", status="ok") for i in range(150)])
    srv = _build_server(monkeypatch, str(db))
    data = _call_audit(srv, start=20)
    assert data["window"] == [20, 120]
    assert len(data["records"]) == 100


def test_handler_missing_file_returns_empty(monkeypatch, tmp_path):
    db = tmp_path / "nope.db"
    srv = _build_server(monkeypatch, str(db))
    data = _call_audit(srv)
    assert data == {"records": [], "total": 0, "tail_size": 0, "window": [0, 0]}


def test_handler_empty_log_path_returns_error(monkeypatch):
    srv = _build_server(monkeypatch, log_path="")
    data = _call_audit(srv)
    assert "error" in data
    assert "not file-backed" in data["error"]


def test_handler_start_beyond_total(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    _seed(db, [_r(1.0, machine="x", action="y", status="ok")])
    srv = _build_server(monkeypatch, str(db))
    data = _call_audit(srv, start=10)
    assert data["records"] == []
    assert data["total"] == 1


def test_handler_tail_param_bounds(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    _seed(db, [_r(float(i), machine="x", action="y", status="ok") for i in range(100)])
    srv = _build_server(monkeypatch, str(db))
    data = _call_audit(srv, tail=10)
    assert data["tail_size"] == 10
    assert data["total"] == 10
    assert [r["ts"] for r in data["records"]] == [float(i) for i in range(90, 100)]


def test_handler_rejects_negative_start(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    _seed(db, [_r(1.0, machine="x", action="y", status="ok")])
    srv = _build_server(monkeypatch, str(db))
    with pytest.raises(ValueError, match=r"start must be"):
        srv._handle_audit_query({"start": -1})


def test_handler_rejects_non_positive_end(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    _seed(db, [_r(1.0, machine="x", action="y", status="ok")])
    srv = _build_server(monkeypatch, str(db))
    with pytest.raises(ValueError, match=r"end must be"):
        srv._handle_audit_query({"end": 0})


def test_handler_rejects_inverted_window(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    _seed(db, [_r(float(i), machine="x", action="y", status="ok") for i in range(5)])
    srv = _build_server(monkeypatch, str(db))
    with pytest.raises(ValueError, match=r"end .* must be > start"):
        srv._handle_audit_query({"start": 4, "end": 2})


def test_handler_rejects_equal_window(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    _seed(db, [_r(float(i), machine="x", action="y", status="ok") for i in range(5)])
    srv = _build_server(monkeypatch, str(db))
    with pytest.raises(ValueError, match=r"end .* must be > start"):
        srv._handle_audit_query({"start": 3, "end": 3})


def test_handler_no_caching_sees_new_records(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    AuditLogger(sink=str(db)).close()
    srv = _build_server(monkeypatch, str(db))

    data1 = _call_audit(srv)
    assert data1["total"] == 0

    AuditLogger(sink=str(db)).record(machine="x", action="between")

    data2 = _call_audit(srv)
    assert data2["total"] == 1
    assert data2["records"][0]["action"] == "between"


# ---------- regression: import-snapshot stale DEFAULT_AUDIT_LOGGER ----------

def test_sandbox_server_audit_not_stale_after_reset(monkeypatch):
    """Regression: SandboxServer.audit must not hold a stale reference to
    ``DEFAULT_AUDIT_LOGGER`` after ``reset_default_logger()`` runs.

    Bug: ``from sandbox_mcp.audit import DEFAULT_AUDIT_LOGGER`` captures
    the import-time instance.  main_http() calls reset_default_logger()
    which closes the old instance and binds a fresh one — but
    SandboxServer.__init__ still sees the closed one because the
    ``from X import Y`` binding in server.py was not updated.

    Symptom in production: every record() silently no-ops (the closed
    logger's record() returns at the ``if self._closed: return`` line),
    so the audit DB stays empty even though the HTTP server is
    processing tool calls.
    """
    from sandbox_mcp import audit as audit_mod
    from sandbox_mcp.audit import reset_default_logger
    from sandbox_mcp.server import SandboxServer

    initial = audit_mod.DEFAULT_AUDIT_LOGGER
    assert initial._closed is False  # sanity

    # Mimic main_http: rebuild the global before constructing the server.
    reset_default_logger()

    fresh = audit_mod.DEFAULT_AUDIT_LOGGER
    assert fresh is not initial
    assert initial._closed is True
    assert fresh._closed is False

    with patch("sandbox_mcp.server.DockerBackend"), patch("sandbox_mcp.server.SSHBackend"):
        srv = SandboxServer()

    # The whole point: self.audit must be the CURRENT global, not the
    # stale snapshot.
    assert srv.audit is fresh
    assert srv.audit._closed is False
