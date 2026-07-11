import io
import json

import pytest

from sandbox_mcp.audit import AuditLogger


@pytest.fixture
def sink():
    return io.StringIO()


def test_record_basic_shape(sink):
    log = AuditLogger(sink=sink)
    log.record(machine="dev", action="shell_exec", status="ok", duration_ms=42, command="ls -la")
    line = sink.getvalue().strip()
    entry = json.loads(line)
    assert entry["machine"] == "dev"
    assert entry["action"] == "shell_exec"
    assert entry["status"] == "ok"
    assert entry["duration_ms"] == 42
    assert entry["details"]["command"] == "ls -la"


def test_record_hashes_content(sink):
    log = AuditLogger(sink=sink)
    log.record(machine="dev", action="file_write", path="/tmp/x.py", content="print('hello')\n")
    entry = json.loads(sink.getvalue().strip())
    details = entry["details"]
    # Raw content must NOT appear in the audit stream.
    assert "content" not in details
    assert "content_sha256" in details
    assert len(details["content_sha256"]) == 16
    assert details["content_len"] == len("print('hello')\n")


def test_record_allows_null_machine(sink):
    log = AuditLogger(sink=sink)
    log.record(machine=None, action="help", status="ok")
    entry = json.loads(sink.getvalue().strip())
    assert entry["machine"] is None
    assert entry["action"] == "help"


def test_record_after_close_is_silent(sink):
    log = AuditLogger(sink=sink)
    log.close()
    log.record(machine="dev", action="shell_exec")
    assert sink.getvalue() == ""


def test_record_swallows_sink_errors(sink):
    class BrokenSink:
        def write(self, *_args, **_kwargs):
            raise OSError("disk full")

        def flush(self):
            pass

    log = AuditLogger(sink=BrokenSink())
    # Should not raise.
    log.record(machine="dev", action="shell_exec")


def test_default_logger_is_disabled_via_close(monkeypatch):
    """The module exposes a default logger; closing it silences output."""
    from sandbox_mcp import audit as audit_module

    captured = io.StringIO()
    monkeypatch.setattr(audit_module, "DEFAULT_AUDIT_LOGGER", AuditLogger(sink=captured))
    audit_module.DEFAULT_AUDIT_LOGGER.record(machine="x", action="y")
    assert "y" in captured.getvalue()
    audit_module.DEFAULT_AUDIT_LOGGER.close()
    audit_module.DEFAULT_AUDIT_LOGGER.record(machine="x", action="z")
    assert "z" not in captured.getvalue()
