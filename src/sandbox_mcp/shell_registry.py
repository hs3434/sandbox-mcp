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

"""Shell registry: tracks all shell sessions across machines."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable

from sandbox_mcp.shell_session import ShellSession, ShellUnhealthy, _health_check


class ShellRegistry:
    """In-memory registry of shell sessions."""

    def __init__(self):
        self._shells: dict[str, dict] = {}
        self._default_shells: dict[str, str] = {}

    def open(self, machine: str, session: ShellSession, purpose: str = "") -> str:
        """Register a session.  Health-checks the session before publishing
        so broken shells are never added to the registry.  Closes the
        session before raising so the caller doesn't have to clean up.
        """
        try:
            _health_check(session)
        except ShellUnhealthy:
            with contextlib.suppress(Exception):
                session.close()
            raise
        shell_id = f"sh_{uuid.uuid4().hex[:12]}"
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
            entry = self._shells[existing]
            # Self-heal: if the default shell has died (e.g. agent ran
            # ``exit`` or it OOM'd), drop it and fall through to create a
            # fresh one.  Otherwise shell_exec would return a confusing
            # "Shell is terminated" error on every call until the agent
            # explicitly notices and calls shell_remove.
            if entry["session"].state != "terminated":
                return existing
            # Capture the dying session's info BEFORE close() — close()
            # nulls out _process, making bash_pid unreadable.  Attach
            # the snapshot to the replacement so the agent sees the
            # previous_shell field on its next shell_exec response.
            prev = _capture_for_replacement(entry["session"])
            self.close(existing)
            session = factory()
            shell_id = self.open(machine, session, purpose="default")
            if prev is not None:
                session.attach_previous_shell(prev)
            self._default_shells[machine] = shell_id
            return shell_id
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
            elif session.state in ("busy", "running"):
                item["bash_pid"] = session.bash_pid
            result.append(item)
        return result

    def count_shells(self, machine: str | None = None) -> int:
        """Count shells without building per-shell dicts.

        Use this when only the count is needed (e.g. ``machine_list``
        summary).  Avoids the O(S) dict allocation in ``list_shells``
        per machine.
        """
        if machine is None:
            return len(self._shells)
        return sum(1 for e in self._shells.values() if e["machine"] == machine)

    def close_all_for_machine(self, machine: str) -> int:
        count = 0
        for sid in [s for s, e in self._shells.items() if e["machine"] == machine]:
            self.close(sid)
            count += 1
        return count


def _capture_for_replacement(dead_session):
    """Snapshot info about a dead session.  Returns None if there's
    nothing meaningful to report (e.g. session never had a real process).

    Called BEFORE ``close()`` so ``bash_pid`` is still readable
    (``close()`` nulls out ``_process``).  The returned dict becomes
    the ``previous_shell`` field on the replacement shell's first
    response.
    """
    if dead_session.bash_pid is None:
        return None
    return {
        "previous_bash_pid": dead_session.bash_pid,
        "last_command": dead_session.last_command,
        "exit_reason": dead_session.exit_reason,
        "exit_code": dead_session.last_exit_code,
    }
