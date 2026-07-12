"""Sandbox MCP server configuration.

Settings are read from environment variables (preferred) or a YAML config
file (optional).  The config file location can be overridden via the
``SANDBOX_MCP_CONFIG`` env var; the default is ``~/.sandbox-mcp/config.yaml``.

Current settings
----------------
- ``work_home`` (env ``SANDBOX_MCP_WORK_HOME``):
  Base directory for per-machine persistent workspaces.
  When a container is created via ``docker_run``, a subdirectory named after
  the machine is created under ``work_home`` and mounted to ``/workspace``
  inside the container.  Default: ``~/.sandbox-mcp/workspaces/``.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_or(key: str, default: str) -> str:
    return os.environ.get(key, default)


def get_work_home() -> Path:
    """Return the resolved path to the workspace root directory."""
    raw = _env_or("SANDBOX_MCP_WORK_HOME", "")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".sandbox-mcp" / "workspaces"


def get_work_dir(name: str) -> Path:
    """Return the per-machine workspace directory, creating it if needed."""
    wd = get_work_home() / name
    wd.mkdir(parents=True, exist_ok=True)
    return wd
