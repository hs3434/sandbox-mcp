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
from unittest.mock import patch

import pytest

from sandbox_mcp.server import SandboxServer


@pytest.fixture
def server():
    with patch("sandbox_mcp.server.DockerBackend"), patch("sandbox_mcp.server.SSHBackend"):
        return SandboxServer()


def test_list_tools_includes_audit_query_by_default(server):
    """With the default config, audit is file-backed, so the tool is exposed."""
    tools = server.list_tools()
    names = {t.name for t in tools}
    expected = {
        "sandbox_shell_exec",
        "sandbox_shell_read",
        "sandbox_file_read",
        "sandbox_file_write",
        "sandbox_file_patch",
        "sandbox_file_search",
        "sandbox_env",
        "sandbox_audit_query",
    }
    assert expected.issubset(names)


def test_list_tools_omits_audit_query_when_log_path_empty(monkeypatch):
    """When [audit] log_path is empty, the audit tool is hidden from agents."""
    monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", "")
    from unittest.mock import patch

    with patch("sandbox_mcp.server.DockerBackend"), patch("sandbox_mcp.server.SSHBackend"):
        srv = SandboxServer()
    names = {t.name for t in srv.list_tools()}
    assert "sandbox_audit_query" not in names
    # Sanity: other tools still present
    assert "sandbox_shell_exec" in names


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


def test_audit_records_inner_action_for_sandbox_env(monkeypatch, tmp_path):
    """``sandbox_env`` is a meta-tool: the inner ``action`` arg is the
    real action and should land in the indexed ``action`` column, not
    the wrapper tool name ``"sandbox_env"``.
    """
    import sqlite3

    from sandbox_mcp.audit import AuditLogger

    db = tmp_path / "audit.db"
    monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", str(db))
    with (
        patch("sandbox_mcp.server.DockerBackend"),
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        srv = SandboxServer(audit=AuditLogger(sink=str(db)))
    # Trigger an audit entry by calling sandbox_env(action="status").
    srv.call_tool("sandbox_env", {"action": "status"})

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT action, details FROM audit").fetchall()
    assert len(rows) == 1
    action, details_json = rows[0]
    # Inner action is recorded at the top column level.
    assert action == "status"
    # ``action`` is filtered out of details (already promoted).
    assert "action" not in (details_json or "{}")


def test_audit_query_does_not_record_itself(monkeypatch, tmp_path):
    """Querying the audit log must not pollute it with self-references."""
    import sqlite3

    from sandbox_mcp.audit import AuditLogger

    db = tmp_path / "audit.db"
    # Pre-existing row from before the query.
    AuditLogger(sink=str(db)).record(machine=None, action="pre_existing")

    monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", str(db))
    with (
        patch("sandbox_mcp.server.DockerBackend"),
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        srv = SandboxServer(audit=AuditLogger(sink=str(db)))

    # Make a query through the tool.
    srv.call_tool("sandbox_audit_query", {})

    with sqlite3.connect(db) as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM audit").fetchall()]
    # Only the pre-existing row is present; the query did not record itself.
    assert actions == ["pre_existing"]
