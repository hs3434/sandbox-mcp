"""Shell registry: tracks all shell sessions across targets."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable

from sandbox_mcp.shell_session import ShellSession


class ShellRegistry:
    """In-memory registry of shell sessions."""

    def __init__(self):
        self._shells: dict[str, dict] = {}
        self._default_shells: dict[str, str] = {}

    def open(self, target: str, session: ShellSession, purpose: str = "") -> str:
        shell_id = f"sh_{uuid.uuid4().hex[:12]}"
        session.purpose = purpose
        self._shells[shell_id] = {
            "session": session,
            "target": target,
            "purpose": purpose,
        }
        return shell_id

    def get(self, shell_id: str) -> ShellSession | None:
        entry = self._shells.get(shell_id)
        return entry["session"] if entry else None

    def get_target(self, shell_id: str) -> str | None:
        entry = self._shells.get(shell_id)
        return entry["target"] if entry else None

    def close(self, shell_id: str) -> bool:
        entry = self._shells.pop(shell_id, None)
        if not entry:
            return False
        with contextlib.suppress(Exception):
            entry["session"].close()
        target = entry["target"]
        if self._default_shells.get(target) == shell_id:
            del self._default_shells[target]
        return True

    def get_or_create_default(self, target: str,
                              factory: Callable[[], ShellSession]) -> str:
        existing = self._default_shells.get(target)
        if existing and existing in self._shells:
            return existing
        session = factory()
        shell_id = self.open(target, session, purpose="default")
        self._default_shells[target] = shell_id
        return shell_id

    def set_default(self, shell_id: str) -> str:
        entry = self._shells.get(shell_id)
        if entry is None:
            raise ValueError(f"Unknown shell_id: {shell_id}")
        target = entry["target"]
        self._default_shells[target] = shell_id
        return target

    def get_default_id(self, target: str) -> str | None:
        return self._default_shells.get(target)

    def list_shells(self, target: str | None = None) -> list[dict]:
        result = []
        for shell_id, entry in self._shells.items():
            if target and entry["target"] != target:
                continue
            session = entry["session"]
            item = {
                "shell_id": shell_id,
                "target": entry["target"],
                "purpose": entry.get("purpose", ""),
                "status": session.state,
                "uptime": f"{int(session.uptime)}s",
                "last_command": session.last_command,
                "is_default": self._default_shells.get(entry["target"]) == shell_id,
            }
            if session.state == "terminated":
                item["hint"] = "Process exited. Call shell_remove to clean up."
            result.append(item)
        return result

    def close_all_for_target(self, target: str) -> int:
        count = 0
        for sid in [s for s, e in self._shells.items() if e["target"] == target]:
            self.close(sid)
            count += 1
        return count
