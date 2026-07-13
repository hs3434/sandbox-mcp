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

"""Tests for the HTTP transport and bearer-token auth middleware."""

from __future__ import annotations

import os

import pytest

from sandbox_mcp.server import _build_http_app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SANDBOX_MCP_"):
            monkeypatch.delenv(key, raising=False)
    yield


class TestBuildHttpApp:
    """Build a Starlette app the same way ``main_http()`` does, then
    probe its routes and middleware via Starlette's TestClient."""

    @staticmethod
    def _build(tokens_file):
        return _build_http_app(tokens_file=tokens_file)

    def test_mounts_mcp_path(self, tmp_path):
        app = self._build(tmp_path / "tokens")
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/mcp" in paths

    def test_no_legacy_sse_path(self, tmp_path):
        app = self._build(tmp_path / "tokens")
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/sse" not in paths
        assert "/messages/" not in paths

    def test_rejects_request_without_auth(self, tmp_path):
        from starlette.testclient import TestClient

        tokens_file = tmp_path / "auth_tokens"
        tokens_file.write_text("test-tok-32-chars-aaaaaaaaaa!\n")
        os.chmod(tokens_file, 0o600)

        app = self._build(tokens_file)
        with TestClient(app) as client:
            resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert resp.status_code == 401
            assert resp.headers.get("www-authenticate") == 'Bearer realm="sandbox-mcp"'

    def test_accepts_request_with_valid_token(self, tmp_path):
        from starlette.testclient import TestClient

        tokens_file = tmp_path / "auth_tokens"
        tokens_file.write_text("test-tok-32-chars-aaaaaaaaaa!\n")
        os.chmod(tokens_file, 0o600)

        app = self._build(tokens_file)
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer test-tok-32-chars-aaaaaaaaaa!"},
            )
            assert resp.status_code != 401

    def test_token_hot_reload(self, tmp_path):
        """Adding a new token to the file takes effect without restart."""
        from starlette.testclient import TestClient

        tokens_file = tmp_path / "auth_tokens"
        tokens_file.write_text("first-token-32-chars-aaaaaa!\n")
        os.chmod(tokens_file, 0o600)

        app = self._build(tokens_file)
        with TestClient(app) as client:
            # First token works
            resp = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer first-token-32-chars-aaaaaa!"},
            )
            assert resp.status_code != 401

            # Second token doesn't work yet
            resp = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
                headers={"Authorization": "Bearer second-token-32-chars-aaaa!"},
            )
            assert resp.status_code == 401

            # Add second token to file
            tokens_file.write_text("first-token-32-chars-aaaaaa!\nsecond-token-32-chars-aaaa!\n")

            # Now second token works (hot-reload)
            resp = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 3, "method": "ping"},
                headers={"Authorization": "Bearer second-token-32-chars-aaaa!"},
            )
            assert resp.status_code != 401
