from unittest.mock import MagicMock

import pytest

from sandbox_mcp.sandbox_env import SandboxEnv


@pytest.fixture
def sandbox_env():
    machines = MagicMock()
    shells = MagicMock()
    docker_backend = MagicMock()
    ssh_backend = MagicMock()
    return SandboxEnv(machines, shells, docker_backend, ssh_backend)


def test_help_returns_operations_and_pointers(sandbox_env):
    result = sandbox_env.dispatch("help", {})
    assert "default_actions" in result
    default_actions = [op["action"] for op in result["default_actions"]]
    assert default_actions == ["help", "status"]
    assert "operations" in result
    actions = [op["action"] for op in result["operations"]]
    assert "machine_list" in actions
    assert "default_set" in actions
    assert "shell_new" in actions
    assert "shell_remove" in actions
    assert "shell_list" in actions
    assert "more_help" in result
    assert "docker_help" in result["more_help"]
    assert "ssh_help" in result["more_help"]


def test_docker_help_returns_docker_ops(sandbox_env):
    result = sandbox_env.dispatch("docker_help", {})
    actions = [op["action"] for op in result["operations"]]
    assert "docker_run" in actions
    assert "docker_build" in actions
    assert "docker_commit" in actions
    assert "docker_stop" in actions
    assert "docker_start" in actions
    assert "docker_remove" in actions


def test_ssh_help_returns_ssh_ops(sandbox_env):
    result = sandbox_env.dispatch("ssh_help", {})
    actions = [op["action"] for op in result["operations"]]
    assert "ssh_connect" in actions
    assert "ssh_disconnect" in actions
    assert "ssh_reconnect" in actions
    assert "ssh_remove" in actions


def test_default_set_sets_default_machine(sandbox_env):
    sandbox_env._machines.resolve_machine.return_value = "dev"
    result = sandbox_env.dispatch("default_set", {"machine": "dev"})
    sandbox_env._machines.set_default.assert_called_once_with("dev")
    assert result == {"default_machine": "dev"}


def test_default_set_sets_default_shell(sandbox_env):
    sandbox_env._shells.get_machine.return_value = "dev"
    result = sandbox_env.dispatch("default_set", {"shell_id": "sh_abc"})
    sandbox_env._shells.get_machine.assert_called_once_with("sh_abc")
    sandbox_env._shells.set_default.assert_called_once_with("sh_abc")
    assert result == {"default_shell": {"machine": "dev", "shell_id": "sh_abc"}}


def test_default_set_rejects_both_machine_and_shell(sandbox_env):
    result = sandbox_env.dispatch("default_set",
                                  {"machine": "dev", "shell_id": "sh_abc"})
    assert "error" in result


def test_machine_list_returns_machines(sandbox_env):
    sandbox_env._machines.list_machines.return_value = ["dev", "db"]
    info_a = MagicMock(name="dev", backend="docker", status="running", purpose="x")
    info_b = MagicMock(name="db", backend="docker", status="running", purpose="y")
    sandbox_env._machines.get_info.side_effect = [info_a, info_b]
    sandbox_env._machines.get_created_at.return_value = 0
    sandbox_env._shells.list_shells.return_value = []
    result = sandbox_env.dispatch("machine_list", {})
    assert "machines" in result
    assert len(result["machines"]) == 2
    assert result["machines"][0]["name"] == "dev"
    assert result["machines"][1]["name"] == "db"


def test_status_returns_state(sandbox_env):
    sandbox_env._machines.get_default.return_value = "dev"
    sandbox_env._machines.list_machines.return_value = ["dev"]
    info = MagicMock(name="dev", backend="docker", status="running",
                     purpose="test", shells=0, uptime="")
    sandbox_env._machines.get_info.return_value = info
    sandbox_env._machines.get_created_at.return_value = 0
    sandbox_env._shells.list_shells.return_value = []
    result = sandbox_env.dispatch("status", {})
    assert result["default_machine"] == "dev"
    assert len(result["machines"]) == 1
    assert "shells" in result


def test_shell_new(sandbox_env):
    backend = MagicMock()
    shell = MagicMock()
    backend.open_shell.return_value = shell
    sandbox_env._machines.resolve_machine.return_value = "dev"
    sandbox_env._machines.get_backend.return_value = backend
    sandbox_env._shells.open.return_value = "sh_abc"
    result = sandbox_env.dispatch("shell_new",
                                  {"machine": "dev", "purpose": "server"})
    backend.open_shell.assert_called_once_with("dev")
    sandbox_env._shells.open.assert_called_once_with("dev", shell,
                                                     purpose="server")
    assert result == {"shell_id": "sh_abc", "machine": "dev"}


def test_shell_remove(sandbox_env):
    sandbox_env._shells.close.return_value = True
    result = sandbox_env.dispatch("shell_remove", {"shell_id": "sh_abc"})
    assert result["status"] == "removed"


def test_shell_list(sandbox_env):
    sandbox_env._shells.list_shells.return_value = [
        {"shell_id": "sh_abc", "machine": "dev", "status": "idle"}
    ]
    result = sandbox_env.dispatch("shell_list", {})
    assert len(result) == 1


def test_docker_run(sandbox_env):
    info = MagicMock(name="dev", backend="docker", status="running", purpose="test")
    sandbox_env._machines.register.return_value = info
    result = sandbox_env.dispatch("docker_run", {
        "name": "dev", "image": "python:3.12", "purpose": "test"
    })
    assert result["status"] == "running"
    assert result["backend"] == "docker"


def test_unknown_action_returns_error(sandbox_env):
    result = sandbox_env.dispatch("nonexistent", {})
    assert "error" in result


def test_missing_required_param_returns_error(sandbox_env):
    result = sandbox_env.dispatch("docker_run", {"name": "dev"})
    assert "error" in result
