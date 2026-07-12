"""Sandbox MCP server configuration.

Settings are read in this priority order (highest first):

1. Environment variables (``SANDBOX_MCP_*``).
2. ``~/.sandbox-mcp/config.toml`` (path overridable via ``SANDBOX_MCP_CONFIG``).
3. Built-in defaults declared as :class:`dataclasses.dataclass` fields below.

For a commented reference of every key, copy ``config.example.toml``
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
    # HTTP transport for the MCP server.  ``streamable-http`` is the
    # current MCP spec direction (single ``/mcp`` endpoint, GET + POST +
    # DELETE); ``sse`` is the legacy HTTP+SSE transport (``/sse`` +
    # ``/messages/``) kept as a fallback for older clients.
    transport: str = "streamable-http"
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
    log_path: str = ""  # "" = stderr


@dataclass(frozen=True)
class DockerConfig:
    container_name_prefix: str = "sandbox-"
    default_image: str = "debian:stable-slim"
    default_workdir: str = "/workspace"
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
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    ssh: SSHConfig = field(default_factory=SSHConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    files: FilesConfig = field(default_factory=FilesConfig)


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
    }

    env_map: dict[str, tuple[str, str, Callable[[str], object]]] = {
        "server_host": ("server", "host", str),
        "server_port": ("server", "port", int),
        "server_transport": ("server", "transport", str),
        "server_auth_tokens_file": ("server", "auth_tokens_file", str),
        "server_auto_generate_if_empty": ("server", "auto_generate_if_empty", _as_bool),
        "storage_work_home": ("storage", "work_home", str),
        "audit_log_path": ("audit", "log_path", str),
        "docker_container_name_prefix": ("docker", "container_name_prefix", str),
        "docker_default_image": ("docker", "default_image", str),
        "docker_default_workdir": ("docker", "default_workdir", str),
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
        "shell_default_max_output": ("shell", "default_max_output", int),
        "shell_head_size": ("shell", "head_size", int),
        "shell_tail_size": ("shell", "tail_size", int),
        "files_max_file_size": ("files", "max_file_size", int),
        "files_max_line_length": ("files", "max_line_length", int),
        "files_default_read_limit": ("files", "default_read_limit", int),
        "files_max_read_limit": ("files", "max_read_limit", int),
        "files_default_search_limit": ("files", "default_search_limit", int),
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
    """Return the per-machine workspace directory, creating it if needed."""
    wd = get_work_home() / name
    wd.mkdir(parents=True, exist_ok=True)
    return wd
