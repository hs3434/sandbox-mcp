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

"""Tests for the TOML config loader and env-var overrides."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sandbox_mcp.config import (
    DefaultMachineConfig,
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
    assert cfg.default_machine == DefaultMachineConfig()
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

[shell]
default_max_output = 1024
"""
    )
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg_file))
    cfg = load()
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 9999
    assert cfg.docker.default_image == "ubuntu:24.04"
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


def test_get_work_dir_returns_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    wd = get_work_dir("mybox")
    assert wd == (tmp_path / "mybox").resolve()
    assert not wd.exists()


def test_get_work_home_default(monkeypatch):
    assert get_work_home() == (Path.home() / ".sandbox-mcp" / "workspaces").resolve()


def test_get_work_dir_uses_env_override(monkeypatch, tmp_path):
    host_dir = tmp_path / "host-ws"
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(host_dir))
    wd = get_work_dir("mybox")
    assert wd == host_dir.resolve() / "mybox"
    assert not wd.exists()


def test_storage_work_home_expands_tilde(monkeypatch):
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", "~/custom-workspaces/")
    cfg = load()
    assert cfg.storage.work_home == (Path.home() / "custom-workspaces").resolve()


def test_repo_example_matches_dataclass_defaults():
    """Drift guard: every key in config/config.example.toml must match the
    dataclass field defaults.  Catches the case where someone bumps a
    default in config.py and forgets to update the human-facing
    reference (or vice versa).
    """
    import tomllib
    from dataclasses import fields

    from sandbox_mcp.config import AppConfig

    repo_root = Path(__file__).resolve().parent.parent
    parsed = tomllib.loads(
        (repo_root / "config" / "config.example.toml").read_text(encoding="utf-8")
    )

    expected: dict[str, dict[str, object]] = {}
    for section_name, section_obj in AppConfig().__dict__.items():
        # ``section_obj`` is a frozen dataclass instance.
        section_dict: dict[str, object] = {}
        for f in fields(section_obj):
            value = getattr(section_obj, f.name)
            # StorageConfig.work_home is resolved to an absolute Path at
            # __post_init__ time; the example file ships the un-resolved
            # "~/.sandbox-mcp/workspaces/" form for portability.
            if section_name == "storage" and f.name == "work_home":
                value = "~/.sandbox-mcp/workspaces/"
            section_dict[f.name] = value
        expected[section_name] = section_dict

    assert parsed == expected, (
        f"config/config.example.toml does not match AppConfig() defaults.\n"
        f"  parsed: {parsed}\n  expected: {expected}"
    )


def test_repo_example_uses_known_sections():
    """Every TOML section must map to a known AppConfig sub-dataclass."""
    import tomllib

    from sandbox_mcp.config import AppConfig

    repo_root = Path(__file__).resolve().parent.parent
    parsed = tomllib.loads(
        (repo_root / "config" / "config.example.toml").read_text(encoding="utf-8")
    )
    valid_sections = set(AppConfig().__dict__.keys())
    assert set(parsed.keys()) == valid_sections, (
        f"Unknown sections in config/config.example.toml: "
        f"{set(parsed.keys()) - valid_sections}; "
        f"missing sections: {valid_sections - set(parsed.keys())}"
    )


# ---- [default_machine] ----


def test_default_machine_defaults_disabled():
    """Default config keeps the historical lazy behaviour (no provisioning).

    [default_machine] holds only the trigger; backend params live in
    their own sections ([docker] default_image, [ssh] default_*).
    """
    dm = DefaultMachineConfig()
    assert dm.enabled is False
    assert dm.backend == "docker"
    assert dm.name == "admin"
    assert dm.purpose == ""
    # No backend-specific fields here anymore.
    assert not hasattr(dm, "image")
    assert not hasattr(dm, "host")

    ssh = SSHConfig()
    assert ssh.default_host == ""
    assert ssh.default_user == ""
    assert ssh.default_port == 22
    assert ssh.default_key == ""


def test_default_machine_load_from_toml(monkeypatch, tmp_path):
    """backend='ssh' target params come from [ssh] default_*, not
    [default_machine]."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[default_machine]
enabled = true
backend = "ssh"
name = "remote-dev"
purpose = "auto-provisioned remote"

[ssh]
default_host = "10.0.0.5"
default_user = "ubuntu"
default_port = 2222
default_key = "/home/ubuntu/.ssh/id_ed25519"
"""
    )
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg_file))
    cfg = load()
    dm = cfg.default_machine
    assert dm.enabled is True
    assert dm.backend == "ssh"
    assert dm.name == "remote-dev"
    assert dm.purpose == "auto-provisioned remote"
    ssh = cfg.ssh
    assert ssh.default_host == "10.0.0.5"
    assert ssh.default_user == "ubuntu"
    assert ssh.default_port == 2222
    assert ssh.default_key == "/home/ubuntu/.ssh/id_ed25519"


def test_default_machine_env_overrides(monkeypatch):
    """[default_machine] trigger fields + [ssh] default_* env overrides."""
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_NAME", "devbox")
    monkeypatch.setenv("SANDBOX_MCP_SSH_DEFAULT_HOST", "10.0.0.5")
    monkeypatch.setenv("SANDBOX_MCP_SSH_DEFAULT_PORT", "2200")
    cfg = load()
    assert cfg.default_machine.enabled is True
    assert cfg.default_machine.name == "devbox"
    assert cfg.ssh.default_host == "10.0.0.5"
    assert cfg.ssh.default_port == 2200


def test_default_machine_env_overrides_file(monkeypatch, tmp_path):
    """Env vars win over the config file, same as every other section."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[default_machine]
enabled = false
name = "from-file"

[ssh]
default_host = "from-file-host"
"""
    )
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg_file))
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_NAME", "from-env")
    monkeypatch.setenv("SANDBOX_MCP_SSH_DEFAULT_HOST", "from-env-host")
    cfg = load()
    assert cfg.default_machine.enabled is True
    assert cfg.default_machine.name == "from-env"
    assert cfg.ssh.default_host == "from-env-host"
