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
    assert data["default_target"] is None
    assert data["targets"] == []
