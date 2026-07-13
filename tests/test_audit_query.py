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
