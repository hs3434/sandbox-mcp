import json
from unittest.mock import patch

import pytest

from sandbox_mcp.server import SandboxServer


@pytest.fixture
def server():
    with patch("sandbox_mcp.server.DockerBackend"), patch("sandbox_mcp.server.SSHBackend"):
        return SandboxServer()


def test_list_tools_returns_7(server):
    tools = server.list_tools()
    assert len(tools) == 7
    names = {t.name for t in tools}
    assert "sandbox_shell_exec" in names
    assert "sandbox_shell_read" in names
    assert "sandbox_file_read" in names
    assert "sandbox_file_write" in names
    assert "sandbox_file_patch" in names
    assert "sandbox_file_search" in names
    assert "sandbox_env" in names


def test_call_unknown_tool(server):
    result = server.call_tool("nonexistent", {})
    data = json.loads(result[0].text)
    assert "error" in data


def test_sandbox_env_help(server):
    result = server.call_tool("sandbox_env", {"action": "help"})
    data = json.loads(result[0].text)
    assert "operations" in data
    assert "more_help" in data


def test_sandbox_env_status_empty(server):
    result = server.call_tool("sandbox_env", {"action": "status"})
    data = json.loads(result[0].text)
    assert data["default_machine"] is None
    assert data["machines"] == []


def test_server_bootstraps_registry_via_docker_ps(monkeypatch):
    """``SandboxServer.__init__`` calls ``docker_ps`` once before serving
    requests so the registry reflects pre-existing labeled containers
    on the daemon.  No separate ``_reconcile_managed_containers``
    function — the existing ``docker_ps`` path IS the refresh.
    """
    from unittest.mock import patch

    with (
        patch("sandbox_mcp.server.DockerBackend") as mock_docker_cls,
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        mock_docker = mock_docker_cls.return_value
        attrs = {"State": {"Status": "running"}, "Config": {"Image": "alpine:3"}}
        mock_docker.list_managed_containers.return_value = [("dev", attrs)]
        srv = SandboxServer()
    # The dispatcher ran docker_ps during init, populating the registry.
    assert srv.machines.list_machines() == ["dev"]
    mock_docker.list_managed_containers.assert_called_once()
    # No create() was ever invoked — adoption only.
    mock_docker.create.assert_not_called()
