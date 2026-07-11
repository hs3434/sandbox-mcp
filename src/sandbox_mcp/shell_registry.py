"""Shell registry: tracks all shell sessions across machines."""

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

    def open(self, machine: str, session: ShellSession, purpose: str = "") -> str:
        shell_id = f"sh_{uuid.uuid4().hex[:12]}"
        session.purpose = purpose
        self._shells[shell_id] = {
            "session": session,
            "machine": machine,
            "purpose": purpose,
        }
        return shell_id

    def get(self, shell_id: str) -> ShellSession | None:
        entry = self._shells.get(shell_id)
        return entry["session"] if entry else None

    def get_machine(self, shell_id: str) -> str | None:
        entry = self._shells.get(shell_id)
        return entry["machine"] if entry else None

    # Backward-compatible alias.
    def get_target(self, shell_id: str) -> str | None:
        return self.get_machine(shell_id)

    def close(self, shell_id: str) -> bool:
        entry = self._shells.pop(shell_id, None)
        if not entry:
            return False
        with contextlib.suppress(Exception):
            entry["session"].close()
        machine = entry["machine"]
        if self._default_shells.get(machine) == shell_id:
            del self._default_shells[machine]
        return True

    def get_or_create_default(self, machine: str, factory: Callable[[], ShellSession]) -> str:
        existing = self._default_shells.get(machine)
        if existing and existing in self._shells:
            return existing
        session = factory()
        shell_id = self.open(machine, session, purpose="default")
        self._default_shells[machine] = shell_id
        return shell_id

    def set_default(self, shell_id: str) -> str:
        entry = self._shells.get(shell_id)
        if entry is None:
            raise ValueError(f"Unknown shell_id: {shell_id}")
        machine = entry["machine"]
        self._default_shells[machine] = shell_id
        return machine

    def get_default_id(self, machine: str) -> str | None:
        return self._default_shells.get(machine)

    def list_shells(self, machine: str | None = None) -> list[dict]:
        result = []
        for shell_id, entry in self._shells.items():
            if machine and entry["machine"] != machine:
                continue
            session = entry["session"]
            item = {
                "shell_id": shell_id,
                "machine": entry["machine"],
                "purpose": entry.get("purpose", ""),
                "status": session.state,
                "uptime": f"{int(session.uptime)}s",
                "last_command": session.last_command,
                "is_default": self._default_shells.get(entry["machine"]) == shell_id,
            }
            if session.state == "terminated":
                item["hint"] = "Process exited. Call shell_remove to clean up."
            result.append(item)
        return result

    def close_all_for_machine(self, machine: str) -> int:
        count = 0
        for sid in [s for s, e in self._shells.items() if e["machine"] == machine]:
            self.close(sid)
            count += 1
        return count

    # Backward-compatible alias.
    def close_all_for_target(self, machine: str) -> int:
        return self.close_all_for_machine(machine)
