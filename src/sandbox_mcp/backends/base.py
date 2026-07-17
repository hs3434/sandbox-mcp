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

"""Abstract backend interface for sandbox execution targets."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sandbox_mcp.shell_session import ShellSession


@dataclass
class TargetInfo:
    name: str
    backend: str  # "docker" | "ssh"
    status: str  # "running" | "stopped" | "error" | "terminated"
    purpose: str = ""
    shells: int = 0
    uptime: str = ""
    error: str = ""
    # Optional backend-supplied metadata.  ``docker`` backend fills
    # ``image`` (tag or short_id) and ``created`` (ISO8601).  ``ssh``
    # backend leaves them empty.  Used by ``docker_ps`` and
    # ``machine_list`` to render rich views without re-querying the daemon.
    image: str = ""
    created: str = ""
    # Human-readable, non-fatal hint for the operator/agent.  Used to
    # surface things like "reattached to an existing container (not a
    # fresh create)" so the agent knows state may have been preserved.
    # Distinct from ``error`` (which marks status="error").
    note: str = ""


class Backend(ABC):
    """Abstract interface for sandbox backends."""

    @abstractmethod
    def create(self, name: str, purpose: str = "", **kwargs) -> TargetInfo:
        """Create and start a new target."""

    @abstractmethod
    def stop(self, name: str) -> TargetInfo:
        """Stop a running target (state preserved)."""

    @abstractmethod
    def start(self, name: str) -> TargetInfo:
        """Start a stopped target."""

    @abstractmethod
    def remove(self, name: str) -> dict:
        """Remove a target entirely."""

    @abstractmethod
    def get_info(self, name: str) -> TargetInfo:
        """Get current status of a target."""

    @abstractmethod
    def open_shell(self, name: str) -> ShellSession:
        """Open a new persistent shell on the target."""

    @abstractmethod
    def exec_oneoff(self, name: str, command: str, timeout: int = 30) -> dict:
        """Execute a one-off command (no persistent shell)."""

    @abstractmethod
    def write_file(self, name: str, path: str, content: bytes) -> dict:
        """Write ``content`` (raw bytes) atomically to ``path`` on the target.

        Backends that have a native file-copy API (e.g. Docker's
        ``put_archive``) should use it; others can stage a temp file via
        ``exec_oneoff`` and ``mv`` it into place. Implementations must
        ensure that a crash mid-write leaves the existing target file
        untouched.
        """
