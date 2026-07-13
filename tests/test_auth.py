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

"""Tests for bearer-token authentication."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sandbox_mcp.auth import (
    _MIN_TOKEN_LEN,
    generate_ephemeral_token,
    load_auth_tokens,
    resolve_tokens_file,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SANDBOX_MCP_"):
            monkeypatch.delenv(key, raising=False)
    yield


class TestResolveTokensFile:
    def test_default_path(self):
        assert resolve_tokens_file() == (Path.home() / ".sandbox-mcp" / "auth_tokens")

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_MCP_SERVER_AUTH_TOKENS_FILE", "/custom/path")
        assert resolve_tokens_file() == Path("/custom/path")

    def test_env_var_expands_tilde(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_MCP_SERVER_AUTH_TOKENS_FILE", "~/custom/auth")
        assert resolve_tokens_file() == (Path.home() / "custom" / "auth")

    def test_config_overrides_default(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[server]\nauth_tokens_file = "/cfg/path"\n')
        monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg))
        assert resolve_tokens_file() == Path("/cfg/path")


class TestLoadAuthTokens:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_auth_tokens(tmp_path / "noexist") == ()

    def test_world_readable_warns_and_returns_empty(self, tmp_path):
        f = tmp_path / "auth_tokens"
        f.write_text("tok1\n", encoding="utf-8")
        # chmod 644
        f.chmod(0o644)
        with pytest.warns(UserWarning, match="too permissive"):
            tokens = load_auth_tokens(f)
        assert tokens == ()

    def test_0600_file_returns_tokens(self, tmp_path):
        f = tmp_path / "auth_tokens"
        f.write_text("tok1\ntok2\n", encoding="utf-8")
        f.chmod(0o600)
        tokens = load_auth_tokens(f)
        assert tokens == ("tok1", "tok2")

    def test_ignores_blank_lines_and_comments(self, tmp_path):
        f = tmp_path / "auth_tokens"
        f.write_text(
            "# comment\ntok1\n\n  # indented comment\ntok2  \n",
            encoding="utf-8",
        )
        f.chmod(0o600)
        tokens = load_auth_tokens(f)
        assert tokens == ("tok1", "tok2")

    def test_short_tokens_warn(self, tmp_path):
        f = tmp_path / "auth_tokens"
        f.write_text("short\nlong_enough_tok_with_32chars!!\n", encoding="utf-8")
        f.chmod(0o600)
        with pytest.warns(UserWarning, match="shorter than"):
            tokens = load_auth_tokens(f)
        assert len(tokens) == 2

    def test_strips_trailing_newlines(self, tmp_path):
        f = tmp_path / "auth_tokens"
        f.write_text("tok1\n", encoding="utf-8")
        f.chmod(0o600)
        assert load_auth_tokens(f) == ("tok1",)


class TestGenerateEphemeralToken:
    def test_length(self):
        tok = generate_ephemeral_token()
        assert len(tok) >= _MIN_TOKEN_LEN

    def test_urlsafe(self):
        tok = generate_ephemeral_token()
        # base64url chars only
        assert all(
            c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in tok
        )

    def test_unique(self):
        assert generate_ephemeral_token() != generate_ephemeral_token()


@pytest.fixture
def token_file(tmp_path):
    """Write a single valid token at 0600 for middleware tests."""
    f = tmp_path / "auth_tokens"
    f.write_text("test-token-abcdef-32-chars-long!\n", encoding="utf-8")
    f.chmod(0o600)
    return f


def test_standalone_middleware_valid_token():
    """Integration-style test: Starlette TestClient with BearerAuthMiddleware."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from sandbox_mcp.server import BearerAuthMiddleware

    app = Starlette(routes=[Route("/", lambda r: PlainTextResponse("ok"))])
    app.add_middleware(BearerAuthMiddleware, tokens=("test-token-abcdef-32-chars-long!",))

    with TestClient(app) as client:
        # Valid token
        resp = client.get("/", headers={"Authorization": "Bearer test-token-abcdef-32-chars-long!"})
        assert resp.status_code == 200

        # Missing header
        resp = client.get("/")
        assert resp.status_code == 401
        assert "missing" in resp.text

        # Wrong token
        resp = client.get("/", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401
        assert "invalid" in resp.text

        # Empty token
        resp = client.get("/", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401
        assert "empty" in resp.text

        # Non-Bearer scheme
        resp = client.get("/", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401
        assert "malformed" in resp.text


def test_standalone_middleware_www_authenticate_header():
    """The 401 response includes a Bearer challenge so MCP clients know to retry."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from sandbox_mcp.server import BearerAuthMiddleware

    app = Starlette(routes=[Route("/", lambda r: PlainTextResponse("ok"))])
    app.add_middleware(BearerAuthMiddleware, tokens=("tok",))

    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.headers.get("www-authenticate") == 'Bearer realm="sandbox-mcp"'
