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

"""Sandbox MCP server configuration.

Settings are read in this priority order (highest first):

1. Environment variables (``SANDBOX_MCP_*``).
2. ``~/.sandbox-mcp/config.toml`` (path overridable via ``SANDBOX_MCP_CONFIG``).
3. Built-in defaults declared as :class:`dataclasses.dataclass` fields below.

For a commented reference of every key, copy ``config/config.example.toml``
from the repo root to ``~/.sandbox-mcp/config.toml``.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path


def _as_bool(raw: str) -> bool:
    """Parse a string env-var value as a bool (1/0/true/false/yes/no/on/off)."""
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_config_path() -> Path:
    raw = os.environ.get("SANDBOX_MCP_CONFIG", "")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".sandbox-mcp" / "config.toml"


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8010
    # Path to the file containing accepted bearer tokens (one per line).
    # Overridable via the [server] table in config.toml or the
    # SANDBOX_MCP_SERVER_AUTH_TOKENS_FILE env var.
    auth_tokens_file: str = "~/.sandbox-mcp/auth_tokens"
    # If true, generate an ephemeral token at startup when the file is
    # missing/empty.  Default false = fail closed (server refuses to
    # start without configured tokens).
    auto_generate_if_empty: bool = True


@dataclass(frozen=True)
class StorageConfig:
    work_home: Path = field(default_factory=lambda: Path.home() / ".sandbox-mcp" / "workspaces")

    def __post_init__(self) -> None:
        # Resolve ~ eagerly so callers always get an absolute path.
        object.__setattr__(self, "work_home", Path(self.work_home).expanduser().resolve())


@dataclass(frozen=True)
class AuditConfig:
    log_path: str = "~/.sandbox-mcp/audit.db"  # default SQLite path; "" = stderr


@dataclass(frozen=True)
class DockerConfig:
    default_image: str = "debian:stable-slim"
    restart_policy_name: str = "on-failure"
    restart_max_retry_count: int = 3
    write_tmp_prefix: str = "/tmp/.sandbox-mcp-write-"
    # User-defined bridge network for DNS-resolvable container-to-container
    # communication.  Created lazily on first docker_run.  Empty = no network.
    auto_network: str = "sandbox-mcp"

    # Docker daemon connection.  Empty ``host`` falls back to ``from_env()``,
    # which reads ``$DOCKER_HOST`` / ``$DOCKER_TLS_VERIFY`` / ``$DOCKER_CERT_PATH``
    # and the docker CLI context.  Setting ``host`` here is equivalent to
    # exporting ``DOCKER_HOST`` for this process — useful when running
    # sandbox-mcp inside a container with the host socket bind-mounted at
    # a non-default path, or when pointing at a remote docker daemon
    # (TCP / TCP+TLS / SSH transport — selected by the ``host`` URL scheme).
    host: str = ""
    tls_verify: bool = False
    cert_path: str = ""


@dataclass(frozen=True)
class SSHConfig:
    connect_timeout: int = 10
    socket_dir_prefix: str = "sandbox-mcp-ssh-"
    tmpfile_pattern: str = ".sandbox-mcp-tmp.XXXXXX"
    # Default SSH target used when [default_machine] backend = "ssh".
    # Leaving ``default_host`` empty disables the SSH default machine.
    # These are connection params only; the machine name/purpose come
    # from [default_machine].
    default_host: str = ""
    default_user: str = ""
    default_port: int = 22
    default_key: str = ""  # empty -> ssh-agent / default key


@dataclass(frozen=True)
class ShellConfig:
    default_max_output: int = 50000
    head_size: int = 5120
    tail_size: int = 46080


@dataclass(frozen=True)
class FilesConfig:
    max_file_size: int = 51200
    max_line_length: int = 2000
    default_read_limit: int = 500
    max_read_limit: int = 2000
    default_search_limit: int = 50


@dataclass(frozen=True)
class DefaultMachineConfig:
    """Opt-in default machine provisioned at startup.

    When ``enabled`` is true, ``SandboxServer.__init__`` provisions a
    machine (via the docker or ssh backend) right after the
    ``docker_ps`` reconciliation pass, so the agent can use
    ``sandbox_shell_exec`` / ``sandbox_file_*`` immediately without an
    explicit ``docker_run`` / ``ssh_connect``.

    Provisioning failure is **fatal**: the operator opted in, so a
    missing default machine would surprise the agent at first use --
    the server refuses to start instead.  Disabled by default to
    preserve the historical lazy behaviour.

    This section holds only the *trigger* (whether, which backend, what
    name).  Backend-specific connection params live in their own
    sections: docker image comes from ``[docker] default_image``; the
    SSH target comes from ``[ssh] default_host`` / ``default_user`` /
    ``default_port`` / ``default_key``.
    """

    enabled: bool = False
    backend: str = "docker"  # "docker" or "ssh"
    name: str = "default"
    purpose: str = ""


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    ssh: SSHConfig = field(default_factory=SSHConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    default_machine: DefaultMachineConfig = field(default_factory=DefaultMachineConfig)


def _apply_env_overrides(cfg: AppConfig) -> AppConfig:
    """Layer ``SANDBOX_MCP_*`` env vars on top of an :class:`AppConfig`."""
    section_overrides: dict[str, dict[str, object]] = {
        "server": {},
        "storage": {},
        "audit": {},
        "docker": {},
        "ssh": {},
        "shell": {},
        "files": {},
        "default_machine": {},
    }

    env_map: dict[str, tuple[str, str, Callable[[str], object]]] = {
        "server_host": ("server", "host", str),
        "server_port": ("server", "port", int),
        "server_auth_tokens_file": ("server", "auth_tokens_file", str),
        "server_auto_generate_if_empty": ("server", "auto_generate_if_empty", _as_bool),
        "storage_work_home": ("storage", "work_home", str),
        "audit_log_path": ("audit", "log_path", str),
        "docker_default_image": ("docker", "default_image", str),
        "docker_restart_policy_name": ("docker", "restart_policy_name", str),
        "docker_restart_max_retry_count": ("docker", "restart_max_retry_count", int),
        "docker_write_tmp_prefix": ("docker", "write_tmp_prefix", str),
        "docker_auto_network": ("docker", "auto_network", str),
        "docker_host": ("docker", "host", str),
        "docker_tls_verify": ("docker", "tls_verify", _as_bool),
        "docker_cert_path": ("docker", "cert_path", str),
        "ssh_connect_timeout": ("ssh", "connect_timeout", int),
        "ssh_socket_dir_prefix": ("ssh", "socket_dir_prefix", str),
        "ssh_tmpfile_pattern": ("ssh", "tmpfile_pattern", str),
        "ssh_default_host": ("ssh", "default_host", str),
        "ssh_default_user": ("ssh", "default_user", str),
        "ssh_default_port": ("ssh", "default_port", int),
        "ssh_default_key": ("ssh", "default_key", str),
        "shell_default_max_output": ("shell", "default_max_output", int),
        "shell_head_size": ("shell", "head_size", int),
        "shell_tail_size": ("shell", "tail_size", int),
        "files_max_file_size": ("files", "max_file_size", int),
        "files_max_line_length": ("files", "max_line_length", int),
        "files_default_read_limit": ("files", "default_read_limit", int),
        "files_max_read_limit": ("files", "max_read_limit", int),
        "files_default_search_limit": ("files", "default_search_limit", int),
        "default_machine_enabled": ("default_machine", "enabled", _as_bool),
        "default_machine_backend": ("default_machine", "backend", str),
        "default_machine_name": ("default_machine", "name", str),
        "default_machine_purpose": ("default_machine", "purpose", str),
    }

    for env_suffix, (section, field_name, coerce) in env_map.items():
        raw = os.environ.get(f"SANDBOX_MCP_{env_suffix.upper()}")
        if raw is None:
            continue
        section_overrides[section][field_name] = coerce(raw)

    def _replace(section: str, current):
        overrides = section_overrides[section]
        if not overrides:
            return current
        return replace(current, **overrides)

    return AppConfig(
        server=_replace("server", cfg.server),
        storage=_replace("storage", cfg.storage),
        audit=_replace("audit", cfg.audit),
        docker=_replace("docker", cfg.docker),
        ssh=_replace("ssh", cfg.ssh),
        shell=_replace("shell", cfg.shell),
        files=_replace("files", cfg.files),
        default_machine=_replace("default_machine", cfg.default_machine),
    )


def _build_from_dict(data: dict) -> AppConfig:
    def section(name: str, cls):
        raw = data.get(name) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Config section [{name}] must be a table")
        # Drop unknown keys silently so future versions stay forward-compatible.
        valid = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in valid})

    return AppConfig(
        server=section("server", ServerConfig),
        storage=section("storage", StorageConfig),
        audit=section("audit", AuditConfig),
        docker=section("docker", DockerConfig),
        ssh=section("ssh", SSHConfig),
        shell=section("shell", ShellConfig),
        files=section("files", FilesConfig),
        default_machine=section("default_machine", DefaultMachineConfig),
    )


def load(path: Path | None = None) -> AppConfig:
    """Load config from ``path`` (default: :func:`_default_config_path`).

    Missing file → built-in defaults.  Env vars always layer on top.
    """
    cfg_path = path or _default_config_path()
    if cfg_path.is_file():
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
        cfg = _build_from_dict(data)
    else:
        cfg = AppConfig()
    return _apply_env_overrides(cfg)


# Backwards-compatible helpers (callers in docker_backend still use these).
def get_work_home() -> Path:
    return load().storage.work_home


def get_work_dir(name: str) -> Path:
    """Return the per-machine workspace directory path for the Docker daemon.

    The directory is NOT created here -- the Docker daemon auto-creates it
    when bind-mounting on container creation.
    """
    return get_work_home() / name
