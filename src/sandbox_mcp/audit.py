"""Structured JSON-line audit logger for sandbox-mcp.

Each call to :meth:`AuditLogger.record` emits a single JSON object on a
newline. File-operation contents are never logged in full — only a
short SHA-256 fingerprint — so the audit stream is safe to ship to
remote log collectors without leaking secrets.

The logger is enabled by default and writes to stderr. Hosts that want
the audit trail to go to a file, a syslog socket, or a sidecar service
can construct their own :class:`AuditLogger` with a different sink and
replace :data:`DEFAULT_AUDIT_LOGGER` before launching the MCP server.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from typing import IO, Any

_CONTENT_KEYS = {"content"}
_BINARY_HASH_LEN = 16


class AuditLogger:
    """Emit JSON-line audit records to a writable sink (default: stderr)."""

    def __init__(self, sink: IO[str] | None = None) -> None:
        self._sink = sink if sink is not None else sys.stderr
        self._closed = False

    def record(self, *, machine: str | None, action: str,
               status: str = "ok", duration_ms: int | None = None,
               **details: Any) -> None:
        """Emit one audit record.

        ``machine`` may be ``None`` for actions that don't apply to a
        specific machine (e.g. ``sandbox_env(action="help")``).
        ``status`` is a short string like ``"ok"`` / ``"error"``.
        ``duration_ms`` is the wall-clock duration of the action.
        Any extra keyword arguments are recorded under ``details``;
        keys named in :data:`_CONTENT_KEYS` are hashed and replaced
        with ``content_sha256`` so raw content never leaks to logs.
        """
        if self._closed:
            return
        entry: dict[str, Any] = {
            "ts": time.time(),
            "machine": machine,
            "action": action,
            "status": status,
            "duration_ms": duration_ms,
        }
        sanitized: dict[str, Any] = {}
        for key, value in details.items():
            if key in _CONTENT_KEYS and isinstance(value, str):
                digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
                sanitized["content_sha256"] = digest[:_BINARY_HASH_LEN]
                sanitized[f"{key}_len"] = len(value)
            else:
                sanitized[key] = value
        if sanitized:
            entry["details"] = sanitized
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str)
            self._sink.write(line + "\n")
            self._sink.flush()
        except Exception:
            # Never let audit logging break the actual tool call.
            pass

    def close(self) -> None:
        self._closed = True


DEFAULT_AUDIT_LOGGER = AuditLogger()


def disable_audit() -> None:
    """No-op stub for backwards compatibility. Audit is always on; to
    silence it, redirect stderr in the host process.
    """
    return
