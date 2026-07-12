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

    def suggest_paths(self, name: str, missing_path: str) -> list:
        """Best-effort fuzzy suggestion for a missing path. Default: empty list."""
        return []
