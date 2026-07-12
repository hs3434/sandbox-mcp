"""Tests for the TOML config loader and env-var overrides."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sandbox_mcp.config import (
    DockerConfig,
    FilesConfig,
    ServerConfig,
    ShellConfig,
    SSHConfig,
    get_work_dir,
    get_work_home,
    load,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Wipe every SANDBOX_MCP_* env var so tests are isolated."""
    for key in list(os.environ):
        if key.startswith("SANDBOX_MCP_"):
            monkeypatch.delenv(key, raising=False)
    yield


def test_load_defaults_when_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(tmp_path / "missing.toml"))
    cfg = load()
    assert cfg.server == ServerConfig()
    assert cfg.docker == DockerConfig()
    assert cfg.ssh == SSHConfig()
    assert cfg.shell == ShellConfig()
    assert cfg.files == FilesConfig()
    assert cfg.storage.work_home == (Path.home() / ".sandbox-mcp" / "workspaces").resolve()


def test_load_from_toml(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[server]
host = "127.0.0.1"
port = 9999

[docker]
default_image = "ubuntu:24.04"
container_name_prefix = "box-"

[shell]
default_max_output = 1024
"""
    )
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg_file))
    cfg = load()
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 9999
    assert cfg.docker.default_image == "ubuntu:24.04"
    assert cfg.docker.container_name_prefix == "box-"
    assert cfg.shell.head_size == ShellConfig.head_size
    assert cfg.shell.default_max_output == 1024


def test_env_var_overrides_file(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[server]
port = 9000

[docker]
default_image = "ubuntu:24.04"
"""
    )
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg_file))
    monkeypatch.setenv("SANDBOX_MCP_SERVER_PORT", "1234")
    monkeypatch.setenv("SANDBOX_MCP_DOCKER_DEFAULT_IMAGE", "alpine:3.20")
    cfg = load()
    assert cfg.server.port == 1234
    assert cfg.docker.default_image == "alpine:3.20"
    assert cfg.docker.container_name_prefix == DockerConfig.container_name_prefix


def test_env_var_alone(monkeypatch):
    monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", "/tmp/audit.log")
    cfg = load()
    assert cfg.audit.log_path == "/tmp/audit.log"


def test_unknown_keys_ignored(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[server]
host = "0.0.0.0"
future_knob = "ignored"
"""
    )
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg_file))
    cfg = load()
    assert cfg.server.host == "0.0.0.0"
    assert not hasattr(cfg.server, "future_knob")


def test_get_work_dir_creates_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    wd = get_work_dir("mybox")
    assert wd == (tmp_path / "mybox").resolve()
    assert wd.is_dir()


def test_get_work_home_default(monkeypatch):
    assert get_work_home() == (Path.home() / ".sandbox-mcp" / "workspaces").resolve()


def test_storage_work_home_expands_tilde(monkeypatch):
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", "~/custom-workspaces/")
    cfg = load()
    assert cfg.storage.work_home == (Path.home() / "custom-workspaces").resolve()
