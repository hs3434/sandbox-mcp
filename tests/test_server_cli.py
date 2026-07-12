"""Tests for the CLI argument parser and override precedence."""

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


def test_stdio_parser_has_only_config_flag():
    parser = _build_arg_parser(prog="sandbox-mcp", with_http=False, description="d")
    # --config should parse, --host should NOT (stdio mode).
    args = parser.parse_args(["--config", "/tmp/c.toml"])
    assert args.config == "/tmp/c.toml"
    assert not hasattr(args, "host")
    assert not hasattr(args, "port")


def test_http_parser_has_config_host_and_port():
    parser = _build_arg_parser(prog="sandbox-mcp-http", with_http=True, description="d")
    args = parser.parse_args(["--config", "/tmp/c.toml", "--host", "1.2.3.4", "--port", "9999"])
    assert args.config == "/tmp/c.toml"
    assert args.host == "1.2.3.4"
    assert args.port == 9999


def test_http_parser_accepts_short_flags():
    parser = _build_arg_parser(prog="sandbox-mcp-http", with_http=True, description="d")
    args = parser.parse_args(["-c", "/etc/foo.toml", "-H", "127.0.0.1", "-p", "1234"])
    assert args.config == "/etc/foo.toml"
    assert args.host == "127.0.0.1"
    assert args.port == 1234


def test_http_parser_port_rejects_non_int():
    parser = _build_arg_parser(prog="sandbox-mcp-http", with_http=True, description="d")
    with pytest.raises(SystemExit):
        parser.parse_args(["--port", "not-a-number"])


def test_apply_cli_config_sets_env_var():
    args = type("A", (), {"config": "/custom/path.toml", "host": None, "port": None})()
    _apply_cli_overrides_to_env(args)
    assert os.environ["SANDBOX_MCP_CONFIG"] == "/custom/path.toml"


def test_apply_cli_host_port_set_env_vars():
    args = type("A", (), {"config": None, "host": "127.0.0.1", "port": 8888})()
    _apply_cli_overrides_to_env(args)
    assert os.environ["SANDBOX_MCP_SERVER_HOST"] == "127.0.0.1"
    assert os.environ["SANDBOX_MCP_SERVER_PORT"] == "8888"


def test_apply_cli_overrides_existing_env_var():
    """CLI flag beats a pre-set env var (highest precedence)."""
    os.environ["SANDBOX_MCP_CONFIG"] = "/from/env.toml"
    args = type("A", (), {"config": "/from/cli.toml", "host": None, "port": None})()
    _apply_cli_overrides_to_env(args)
    assert os.environ["SANDBOX_MCP_CONFIG"] == "/from/cli.toml"


def test_apply_cli_with_no_flags_does_nothing():
    before = dict(os.environ)
    args = type("A", (), {"config": None, "host": None, "port": None})()
    _apply_cli_overrides_to_env(args)
    assert dict(os.environ) == before


def test_apply_cli_partial_only_sets_what_provided():
    """--config without --host shouldn't blow away $SANDBOX_MCP_SERVER_HOST."""
    os.environ["SANDBOX_MCP_SERVER_HOST"] = "10.0.0.1"
    args = type("A", (), {"config": "/x.toml", "host": None, "port": None})()
    _apply_cli_overrides_to_env(args)
    assert os.environ["SANDBOX_MCP_CONFIG"] == "/x.toml"
    assert os.environ["SANDBOX_MCP_SERVER_HOST"] == "10.0.0.1"


def test_end_to_end_config_via_cli(tmp_path, monkeypatch):
    """A custom config path passed to _apply_cli_overrides_to_env is
    picked up by config.load() — the same flow main() uses."""
    from sandbox_mcp.config import load

    cfg = tmp_path / "custom.toml"
    cfg.write_text("[server]\nport = 12345\n")
    args = type("A", (), {"config": str(cfg), "host": None, "port": None})()
    _apply_cli_overrides_to_env(args)
    loaded = load()
    assert loaded.server.port == 12345
