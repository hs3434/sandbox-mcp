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
    assert "docker_ps" in actions
    assert "docker_images" in actions


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
    result = sandbox_env.dispatch("default_set", {"machine": "dev", "shell_id": "sh_abc"})
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
    info = MagicMock(
        name="dev", backend="docker", status="running", purpose="test", shells=0, uptime=""
    )
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
    result = sandbox_env.dispatch("shell_new", {"machine": "dev", "purpose": "server"})
    backend.open_shell.assert_called_once_with("dev")
    sandbox_env._shells.open.assert_called_once_with("dev", shell, purpose="server")
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
    result = sandbox_env.dispatch(
        "docker_run", {"name": "dev", "image": "python:3.12", "purpose": "test"}
    )
    assert result["status"] == "running"
    assert result["backend"] == "docker"


def test_docker_run_surfaces_reattach_note_and_error(sandbox_env):
    """docker_run's response carries the backend's non-fatal note (e.g. a
    409 reattach) and any error diagnostic, so the agent can tell a fresh
    create from a reuse and see why a container failed to stay running.
    """
    from sandbox_mcp.backends.base import TargetInfo

    # Reattach success: running + note, no error.
    info = TargetInfo(
        name="dev",
        backend="docker",
        status="running",
        note="reattached to existing container (already running)",
    )
    sandbox_env._machines.register.return_value = info
    result = sandbox_env.dispatch(
        "docker_run", {"name": "dev", "image": "python:3.12", "purpose": "test"}
    )
    assert result["status"] == "running"
    assert result["note"] == "reattached to existing container (already running)"
    assert "error" not in result

    # Failed to stay running: error + status, no note.
    info = TargetInfo(
        name="dev", backend="docker", status="error", error="container is 'exited' after start"
    )
    sandbox_env._machines.register.return_value = info
    result = sandbox_env.dispatch(
        "docker_run", {"name": "dev", "image": "python:3.12", "purpose": "test"}
    )
    assert result["status"] == "error"
    assert result["error"] == "container is 'exited' after start"
    assert "note" not in result


def test_docker_commit_requires_image_tag(sandbox_env):
    """docker_commit must reject calls without image_tag (no auto-default)."""
    result = sandbox_env.dispatch("docker_commit", {"machine": "dev"})
    assert "error" in result
    assert "image_tag" in result["error"]


def test_docker_commit_passes_image_tag(sandbox_env):
    from sandbox_mcp.backends.docker_backend import DockerBackend

    backend = MagicMock(spec=DockerBackend)
    backend.commit.return_value = {"image_tag": "myapp:v1", "status": "committed"}
    sandbox_env._machines.resolve_machine.return_value = "dev"
    sandbox_env._machines.get_backend.return_value = backend
    result = sandbox_env.dispatch("docker_commit", {"machine": "dev", "image_tag": "myapp:v1"})
    backend.commit.assert_called_once_with("dev", "myapp:v1")
    assert result["status"] == "committed"


def test_unknown_action_returns_error(sandbox_env):
    result = sandbox_env.dispatch("nonexistent", {})
    assert "error" in result


def test_missing_required_param_returns_error(sandbox_env):
    result = sandbox_env.dispatch("docker_run", {"name": "dev"})
    assert "error" in result


def test_docker_ps_returns_container_list(sandbox_env):
    """`docker_ps` is both the refresh and the list operation.

    It queries the daemon for labeled containers, adopts each one into
    the registry (idempotent), and returns the list.  A first call after
    server start populates the registry; subsequent calls just refresh.
    """
    managed = [
        (
            "dev",
            {
                "State": {"Status": "running"},
                "Created": "2026-01-01",
                "Config": {"Image": "alpine:3"},
            },
        ),
        (
            "db",
            {
                "State": {"Status": "running"},
                "Created": "2026-01-02",
                "Config": {"Image": "postgres:16"},
            },
        ),
    ]
    sandbox_env._docker.list_managed_containers.return_value = managed
    result = sandbox_env.dispatch("docker_ps", {})
    assert [c["name"] for c in result["containers"]] == ["db", "dev"]
    # Each managed container was adopted into the registry.
    assert sandbox_env._machines.adopt.call_count == 2


def test_docker_ps_reads_purpose_from_label(sandbox_env):
    """purpose is persisted as a ``sandbox-mcp.purpose`` docker label and
    read back during reconciliation, so it survives restarts."""
    managed = [
        (
            "dev",
            {
                "State": {"Status": "running"},
                "Created": "2026-01-01",
                "Config": {
                    "Image": "alpine:3",
                    "Labels": {
                        "sandbox-mcp.managed": "true",
                        "sandbox-mcp.machine": "dev",
                        "sandbox-mcp.purpose": "Python dev box",
                    },
                },
            },
        ),
    ]
    sandbox_env._docker.list_managed_containers.return_value = managed
    result = sandbox_env.dispatch("docker_ps", {})
    assert result["containers"][0]["purpose"] == "Python dev box"
    # adopt received a TargetInfo carrying the persisted purpose.
    sandbox_env._machines.adopt.assert_called_once()
    adopted_info = sandbox_env._machines.adopt.call_args.args[2]
    assert adopted_info.purpose == "Python dev box"


def test_docker_ps_purpose_empty_when_no_label(sandbox_env):
    """A container without the purpose label reconciles with purpose=''."""
    managed = [
        (
            "dev",
            {
                "State": {"Status": "running"},
                "Created": "2026-01-01",
                "Config": {"Image": "alpine:3", "Labels": {"sandbox-mcp.machine": "dev"}},
            },
        ),
    ]
    sandbox_env._docker.list_managed_containers.return_value = managed
    result = sandbox_env.dispatch("docker_ps", {})
    assert result["containers"][0]["purpose"] == ""


def test_docker_ps_refresh_is_idempotent(sandbox_env):
    """A second call with no new containers on the daemon is a no-op
    for the registry (adopt is idempotent)."""
    sandbox_env._machines.list_machines.return_value = ["dev"]
    sandbox_env._machines.adopt.side_effect = None  # record calls
    attrs = {"State": {"Status": "running"}, "Config": {"Image": "alpine:3"}}
    sandbox_env._docker.list_managed_containers.return_value = [("dev", attrs)]
    sandbox_env.dispatch("docker_ps", {})
    sandbox_env.dispatch("docker_ps", {})
    # adopt is called every time — that's fine, TargetRegistry.adopt
    # no-ops on already-known names.
    assert sandbox_env._machines.adopt.call_count == 2


def test_docker_ps_ignores_unlabeled_containers(sandbox_env):
    """Daemon may host many containers — only ``sandbox-mcp.managed=true``
    ones surface.  The list_managed_containers backend call is what
    enforces this; the dispatcher just passes the result through.
    """
    # Backend already filtered out unlabeled ones in this mock — but we
    # assert the dispatcher doesn't add its own filter on top.
    sandbox_env._docker.list_managed_containers.return_value = []
    result = sandbox_env.dispatch("docker_ps", {})
    assert result == {"containers": []}
    sandbox_env._docker.list_managed_containers.assert_called_once()


def test_docker_images_returns_images(sandbox_env):
    sandbox_env._docker.list_images.return_value = [
        {"tag": "python:3.12", "image_id": "sha256:abc", "created": "", "size_mb": 120.5},
    ]
    result = sandbox_env.dispatch("docker_images", {})
    assert "images" in result
    assert result["images"][0]["tag"] == "python:3.12"


def test_docker_ps_returns_managed_containers_only(sandbox_env):
    """The agent used to be able to enumerate every host container via
    ``name_prefix=""``.  The new ``docker_ps`` calls the backend's
    label-filtered ``list_managed_containers`` — which never sees the
    host's full inventory — and adopts each into the registry.
    """
    sandbox_env._docker.list_managed_containers.return_value = [
        (
            "dev",
            {"State": {"Status": "running"}, "Config": {"Image": "alpine:3"}},
        ),
    ]
    result = sandbox_env.dispatch("docker_ps", {})
    assert [c["name"] for c in result["containers"]] == ["dev"]
    sandbox_env._docker.list_managed_containers.assert_called_once()
    # Old fingerprinting path is dead.
    sandbox_env._docker.list_containers.assert_not_called()
