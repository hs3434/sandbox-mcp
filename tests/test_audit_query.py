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
    DEFAULT_TAIL,
    MAX_TAIL,
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


def test_read_tail_lines_rejects_zero(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"tail must be in"):
        read_tail_lines(p, 0)


def test_read_tail_lines_rejects_negative(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"tail must be in"):
        read_tail_lines(p, -1)


def test_read_tail_lines_rejects_over_cap(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"tail must be in"):
        read_tail_lines(p, MAX_TAIL + 1)


def test_read_tail_lines_accepts_cap(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("x\n", encoding="utf-8")
    assert read_tail_lines(p, MAX_TAIL) == ["x\n"]


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
    assert any("malformed" in rec.message.lower() for rec in caplog.records)


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
        _r(1.0, action="shell_exec", machine="dev", status="ok"),
        _r(2.0, action="shell_exec", machine="dev", status="error"),
        _r(3.0, action="shell_exec", machine="prod", status="error"),
        _r(4.0, action="file_read", machine="dev", status="error"),
    ]
    out = list(apply_filters(
        recs, action="shell_exec", machine="dev", status="error", since=1.5,
    ))
    assert [r["ts"] for r in out] == [2.0]


# ---------- constants ----------

def test_default_tail_is_5000():
    assert DEFAULT_TAIL == 5000


def test_max_tail_is_100000():
    assert MAX_TAIL == 100_000


# ---------- handler integration ----------


def _seed_audit_log(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _build_server(monkeypatch, log_path):
    """Build a SandboxServer with a controlled audit config."""
    from unittest.mock import patch

    from sandbox_mcp.server import SandboxServer as _Srv

    if log_path is not None:
        monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", log_path)
    with patch("sandbox_mcp.server.DockerBackend"), patch("sandbox_mcp.server.SSHBackend"):
        return _Srv()


def _call_audit(server, **kwargs):
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
    assert page3["window"] == [8, 10]


def test_handler_end_defaults_to_start_plus_100(monkeypatch, tmp_path):
    log = tmp_path / "audit.log"
    _seed_audit_log(log, [
        {"ts": float(i), "machine": "dev", "action": "x", "status": "ok"} for i in range(5)
    ])
    srv = _build_server(monkeypatch, str(log))
    data = _call_audit(srv, start=2)
    assert data["window"] == [2, 5]


def test_handler_missing_file_returns_empty(monkeypatch, tmp_path):
    log = tmp_path / "nope.log"
    srv = _build_server(monkeypatch, str(log))
    data = _call_audit(srv)
    assert data == {"records": [], "total": 0, "tail_size": 0, "window": [0, 0]}


def test_handler_empty_log_path_returns_error(monkeypatch):
    srv = _build_server(monkeypatch, log_path="")
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
