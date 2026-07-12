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


def test_server_reconciles_managed_containers_at_startup(monkeypatch):
    """A fresh ``SandboxServer`` must adopt pre-existing labeled
    containers on startup so state survives restart.  Without
    reconciliation, the agent loses its view of every container it
    created in the previous process and the namespace boundary
    disappears until the agent re-registers each one by name.
    """
    from unittest.mock import MagicMock, patch

    fake_container = MagicMock()
    fake_container.labels = {
        "sandbox-mcp.managed": "true",
        "sandbox-mcp.machine": "dev",
    }
    fake_container.attrs = {
        "Created": "2026-01-01T00:00:00Z",
        "State": {"Status": "running"},
    }

    with (
        patch("sandbox_mcp.server.DockerBackend") as mock_docker_cls,
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        mock_docker = mock_docker_cls.return_value
        mock_docker.list_managed_containers.return_value = [("dev", fake_container.attrs)]
        srv = SandboxServer()
    # Reconciled machine is in the registry, with running status, no
    # create() call.
    assert srv.machines.list_machines() == ["dev"]
    mock_docker.create.assert_not_called()
    info = srv.machines.get_info("dev")
    assert info.status == "running"
