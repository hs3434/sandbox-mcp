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

"""Tests for HTTP transport selection in sandbox-mcp-http.

Covers the ``--transport`` CLI flag and the ``_build_http_app()`` factory
that produces a Starlette ASGI app for either Streamable HTTP (``/mcp``)
or the legacy HTTP+SSE transport (``/sse`` + ``/messages/``).
"""

from __future__ import annotations

import os

import pytest

from sandbox_mcp.server import _apply_cli_overrides_to_env, _build_arg_parser


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SANDBOX_MCP_"):
            monkeypatch.delenv(key, raising=False)
    yield


class TestTransportFlag:
    def test_default_is_none_so_config_wins(self):
        """No parser default: leaves the choice to env / config / default.

        argparse defaulting to a value would force the env var to be set
        and clobber the [server] transport config-file value, breaking the
        CLI > env > config > default precedence chain.
        """
        parser = _build_arg_parser(prog="sandbox-mcp-http", with_http=True, description="d")
        args = parser.parse_args([])
        assert args.transport is None

    def test_server_config_default_is_streamable_http(self):
        """When nothing overrides it, ServerConfig defaults to the new transport."""
        from sandbox_mcp.config import ServerConfig

        assert ServerConfig().transport == "streamable-http"

    def test_accepts_sse_explicit(self):
        parser = _build_arg_parser(prog="sandbox-mcp-http", with_http=True, description="d")
        args = parser.parse_args(["--transport", "sse"])
        assert args.transport == "sse"

    def test_accepts_streamable_http_explicit(self):
        parser = _build_arg_parser(prog="sandbox-mcp-http", with_http=True, description="d")
        args = parser.parse_args(["--transport", "streamable-http"])
        assert args.transport == "streamable-http"

    def test_rejects_unknown_transport(self):
        parser = _build_arg_parser(prog="sandbox-mcp-http", with_http=True, description="d")
        with pytest.raises(SystemExit):
            parser.parse_args(["--transport", "websocket"])

    def test_stdio_parser_has_no_transport_flag(self):
        """--transport only makes sense for the HTTP server."""
        parser = _build_arg_parser(prog="sandbox-mcp", with_http=False, description="d")
        args = parser.parse_args([])
        assert not hasattr(args, "transport")


class TestBuildHttpApp:
    """Build a Starlette app the same way ``main_http()`` does, then
    probe its routes and middleware via Starlette's TestClient."""

    @staticmethod
    def _build(transport: str):
        from sandbox_mcp.server import _build_http_app

        return _build_http_app(transport=transport, tokens=("test-tok-32-chars-aaaaaaaaaa!",))

    def test_streamable_http_mounts_mcp_path(self):
        app = self._build("streamable-http")
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/mcp" in paths

    def test_streamable_http_has_no_legacy_sse_path(self):
        app = self._build("streamable-http")
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/sse" not in paths
        assert "/messages/" not in paths

    def test_sse_mounts_legacy_paths(self):
        app = self._build("sse")
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/sse" in paths
        assert "/messages/" in paths

    def test_sse_has_no_mcp_path(self):
        app = self._build("sse")
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/mcp" not in paths

    def test_unknown_transport_raises(self):
        with pytest.raises(ValueError, match="Unknown transport"):
            self._build("websocket")

    def test_streamable_http_rejects_request_without_auth(self):
        from starlette.testclient import TestClient

        app = self._build("streamable-http")
        with TestClient(app) as client:
            resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert resp.status_code == 401
            assert resp.headers.get("www-authenticate") == 'Bearer realm="sandbox-mcp"'

    def test_sse_rejects_request_without_auth(self):
        from starlette.testclient import TestClient

        app = self._build("sse")
        with TestClient(app) as client:
            resp = client.get("/sse")
            assert resp.status_code == 401

    def test_streamable_http_accepts_request_with_valid_token(self):
        from starlette.testclient import TestClient

        app = self._build("streamable-http")
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer test-tok-32-chars-aaaaaaaaaa!"},
            )
            assert resp.status_code != 401


class TestApplyCliTransport:
    def test_apply_cli_transport_sets_env_var(self):
        args = type(
            "A",
            (),
            {
                "config": None,
                "host": None,
                "port": None,
                "transport": "sse",
            },
        )()
        _apply_cli_overrides_to_env(args)
        assert os.environ["SANDBOX_MCP_SERVER_TRANSPORT"] == "sse"

    def test_apply_cli_no_transport_does_not_set_env(self):
        """When --transport is not passed (None), config/default should win."""
        before = dict(os.environ)
        args = type(
            "A",
            (),
            {"config": None, "host": None, "port": None, "transport": None},
        )()
        _apply_cli_overrides_to_env(args)
        assert "SANDBOX_MCP_SERVER_TRANSPORT" not in os.environ
        assert os.environ == before
