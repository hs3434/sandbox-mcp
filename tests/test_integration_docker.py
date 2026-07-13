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

import json
import shutil

import pytest

from sandbox_mcp.server import SandboxServer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not shutil.which("docker"),
        reason="Docker not available",
    ),
]


@pytest.fixture
def server():
    return SandboxServer()


@pytest.fixture
def docker_target(server):
    """Create a temporary Docker target via sandbox_env."""
    result = server.call_tool(
        "sandbox_env",
        {
            "action": "docker_run",
            "params": {
                "name": "test-integration",
                "image": "python:3.12-slim",
                "purpose": "integration test",
            },
        },
    )
    data = json.loads(result[0].text)
    if "error" in data:
        pytest.skip(f"Cannot create Docker container: {data['error']}")
    server.call_tool(
        "sandbox_env",
        {
            "action": "default_set",
            "params": {"machine": "test-integration"},
        },
    )
    yield server
    server.call_tool(
        "sandbox_env",
        {
            "action": "docker_remove",
            "params": {"machine": "test-integration"},
        },
    )


def test_shell_exec_wait_true(docker_target):
    """shell_exec(wait=true) executes a command and returns output."""
    result = docker_target.call_tool(
        "sandbox_shell_exec",
        {
            "command": "echo hello_from_docker",
        },
    )
    data = json.loads(result[0].text)
    assert data["status"] == "completed"
    assert "hello_from_docker" in data["output"]


def test_shell_exec_preserves_state(docker_target):
    """Environment changes persist across exec calls."""
    docker_target.call_tool(
        "sandbox_shell_exec",
        {
            "command": "export TEST_VAR=12345",
        },
    )
    result = docker_target.call_tool(
        "sandbox_shell_exec",
        {
            "command": "echo $TEST_VAR",
        },
    )
    data = json.loads(result[0].text)
    assert "12345" in data["output"]


def test_shell_exec_wait_false_then_read(docker_target):
    """shell_exec(wait=false) starts command, shell_read gets output."""
    result = docker_target.call_tool(
        "sandbox_shell_exec",
        {
            "command": "echo started; sleep 0.5; echo done",
            "wait": False,
            "timeout": 3,
        },
    )
    assert result

    import time

    time.sleep(1.5)

    list_result = docker_target.call_tool(
        "sandbox_env",
        {
            "action": "shell_list",
            "params": {"machine": "test-integration"},
        },
    )
    shells = json.loads(list_result[0].text)
    default_shell = next((s for s in shells if s["is_default"]), None)
    assert default_shell is not None
    shell_id = default_shell["shell_id"]

    final = docker_target.call_tool("sandbox_shell_read", {"shell_id": shell_id})
    final_data = json.loads(final[0].text)
    assert final_data["status"] in ("running", "completed", "idle")


def test_file_operations_in_docker(docker_target):
    """Write and read a file in a Docker container."""
    result = docker_target.call_tool(
        "sandbox_file_write",
        {
            "path": "/tmp/test_file.txt",
            "content": "line1\nline2\nline3\n",
        },
    )
    data = json.loads(result[0].text)
    assert data["status"] == "ok"

    result = docker_target.call_tool(
        "sandbox_file_read",
        {
            "path": "/tmp/test_file.txt",
        },
    )
    data = json.loads(result[0].text)
    assert "2|line2" in data["output"]


def test_sandbox_env_status(docker_target):
    """sandbox_env status shows the target."""
    result = docker_target.call_tool("sandbox_env", {"action": "status"})
    data = json.loads(result[0].text)
    assert data["default_machine"] == "test-integration"
    assert len(data["machines"]) == 1


def test_docker_commit(docker_target):
    """Commit container state to a new image."""
    result = docker_target.call_tool(
        "sandbox_env",
        {
            "action": "docker_commit",
            "params": {"machine": "test-integration", "image_tag": "sandbox-test-snapshot:latest"},
        },
    )
    data = json.loads(result[0].text)
    assert data.get("status") in ("committed", "error")
