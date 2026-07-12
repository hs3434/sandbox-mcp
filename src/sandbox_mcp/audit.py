"""Structured JSON-line audit logger for sandbox-mcp.

Each call to :meth:`AuditLogger.record` emits a single JSON object on a
newline. File-operation contents are never logged in full — only a
short SHA-256 fingerprint — so the audit stream is safe to ship to
remote log collectors without leaking secrets.

The default sink is stderr.  Operators can redirect the audit trail to
a file via the ``[audit] log_path`` setting in
``~/.sandbox-mcp/config.toml`` (or the ``SANDBOX_MCP_AUDIT_LOG_PATH``
env var).  Set ``log_path`` to an empty string to keep the default
stderr behaviour.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import IO, Any

from sandbox_mcp.config import load as _load_config

_CONTENT_KEYS = {"content"}
_BINARY_HASH_LEN = 16


def _open_sink(log_path: str) -> IO[str]:
    """Return a writable text stream for audit records.

    Empty / unset ``log_path`` ⇒ stderr (default).
    Non-empty path is opened in append mode; the parent directory is
    created on demand.
    """
    if not log_path:
        return sys.stderr
    p = Path(log_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p.open("a", encoding="utf-8")


class AuditLogger:
    """Emit JSON-line audit records to a writable sink (default: stderr)."""

    def __init__(self, sink: IO[str] | str | None = None) -> None:
        """Construct an audit logger.

        ``sink`` may be:
        - a writable text stream (e.g. ``sys.stderr``, an open file);
        - a string path (``""`` ⇒ stderr, otherwise append-mode file);
        - ``None`` ⇒ read the configured ``[audit] log_path``.
        """
        if sink is None:
            sink = _load_config().audit.log_path
        if isinstance(sink, str):
            sink = _open_sink(sink)
        self._sink = sink if sink is not None else sys.stderr
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


def reset_default_logger(sink: IO[str] | str | None = None) -> AuditLogger:
    """Rebuild :data:`DEFAULT_AUDIT_LOGGER` (used at server startup to pick
    up ``[audit] log_path`` after env or config changes).
    """
    global DEFAULT_AUDIT_LOGGER
    with contextlib.suppress(Exception):
        DEFAULT_AUDIT_LOGGER.close()
    DEFAULT_AUDIT_LOGGER = AuditLogger(sink)
    return DEFAULT_AUDIT_LOGGER


def disable_audit() -> None:
    """No-op stub for backwards compatibility. Audit is always on; to
    silence it, redirect stderr in the host process.
    """
    return
