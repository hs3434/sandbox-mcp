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

"""Sandbox MCP Server: 7 tools (6 core + 1 sandbox_env entry).

Tools exposed via MCP tools/list:
  - sandbox_shell_exec
  - sandbox_shell_read
  - sandbox_file_read
  - sandbox_file_write
  - sandbox_file_patch
  - sandbox_file_search
  - sandbox_env  (progressive discovery)

CLI flags
---------

Both ``sandbox-mcp`` (stdio) and ``sandbox-mcp-http`` (HTTP) accept:

- ``--config PATH`` / ``-c PATH``: path to a TOML config file (defaults to
  ``~/.sandbox-mcp/config.toml``).  Overrides ``SANDBOX_MCP_CONFIG``.
- ``--host ADDR``: HTTP bind address (HTTP mode only).  Overrides the
  ``[server] host`` value and ``SANDBOX_MCP_SERVER_HOST``.
- ``--port N``: HTTP port (HTTP mode only).  Overrides the ``[server] port``
  value and ``SANDBOX_MCP_SERVER_PORT``.

Precedence (highest first): CLI flag → env var → config file → built-in default.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import time
from pathlib import Path

from sandbox_mcp.audit import (
    DEFAULT_AUDIT_LOGGER,
    DEFAULT_TAIL,
    AuditLogger,
    query_audit,
    reset_default_logger,
)
from sandbox_mcp.backends.docker_backend import DockerBackend
from sandbox_mcp.backends.ssh_backend import SSHBackend
from sandbox_mcp.config import load as _load_config
from sandbox_mcp.file_operations import FileOperations
from sandbox_mcp.sandbox_env import SandboxEnv
from sandbox_mcp.shell_registry import ShellRegistry
from sandbox_mcp.target_registry import TargetRegistry

logger = logging.getLogger(__name__)


def _build_arg_parser(*, prog: str, with_http: bool, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "--config",
        "-c",
        help=(
            "Path to config.toml. Overrides $SANDBOX_MCP_CONFIG and "
            "the default ~/.sandbox-mcp/config.toml."
        ),
    )
    if with_http:
        parser.add_argument(
            "--host",
            "-H",
            help="HTTP bind address. Overrides [server] host and $SANDBOX_MCP_SERVER_HOST.",
        )
        parser.add_argument(
            "--port",
            "-p",
            type=int,
            help="HTTP port. Overrides [server] port and $SANDBOX_MCP_SERVER_PORT.",
        )
    return parser


def _apply_cli_overrides_to_env(args: argparse.Namespace) -> None:
    """Translate CLI flags into SANDBOX_MCP_* env vars so the rest of the
    config pipeline (which only reads env vars + config file) sees them.

    CLI wins over any pre-set env var.
    """
    if args.config:
        os.environ["SANDBOX_MCP_CONFIG"] = args.config
    if getattr(args, "host", None):
        os.environ["SANDBOX_MCP_SERVER_HOST"] = args.host
    if getattr(args, "port", None) is not None:
        os.environ["SANDBOX_MCP_SERVER_PORT"] = str(args.port)


TOOL_DEFINITIONS = [
    {
        "name": "sandbox_shell_exec",
        "description": (
            "Execute a shell command. wait=true (default) blocks until "
            "completion or timeout. wait=false returns after "
            "confirming execution started."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "shell_id": {
                    "type": "string",
                    "description": "Specific shell (default: machine's default shell)",
                },
                "machine": {
                    "type": "string",
                    "description": "Machine name (default: default machine)",
                },
                "wait": {"type": "boolean", "description": "Wait for completion (default: true)"},
                "timeout": {"type": "integer", "description": "Seconds to wait (default: 30)"},
                "max_output": {
                    "type": "integer",
                    "description": "Max output bytes (default: 50000)",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "sandbox_shell_read",
        "description": "Read new output from a shell (non-blocking). Detects "
        "command completion via markers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "shell_id": {"type": "string", "description": "Shell to read from"},
            },
            "required": ["shell_id"],
        },
    },
    {
        "name": "sandbox_file_read",
        "description": "Read a text file with line numbers and pagination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "machine": {
                    "type": "string",
                    "description": "Machine name (default: default machine)",
                },
                "offset": {"type": "integer", "description": "Start line (1-indexed, default: 1)"},
                "limit": {"type": "integer", "description": "Max lines (default: 500, max: 2000)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "sandbox_file_write",
        "description": (
            "Write content to a file, replacing existing. "
            "Creates parent dirs. Runs syntax check for known "
            "extensions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "Complete file content"},
                "machine": {
                    "type": "string",
                    "description": "Machine name (default: default machine)",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "sandbox_file_patch",
        "description": "Targeted find-and-replace edits with fuzzy matching. "
        "mode=replace or mode=patch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["replace", "patch"]},
                "path": {"type": "string", "description": "File path (replace mode)"},
                "old_string": {"type": "string", "description": "Text to find (replace mode)"},
                "new_string": {"type": "string", "description": "Replacement text (replace mode)"},
                "replace_all": {"type": "boolean", "description": "Replace all (default: false)"},
                "patch": {"type": "string", "description": "Patch content (patch mode)"},
                "machine": {
                    "type": "string",
                    "description": "Machine name (default: default machine)",
                },
            },
            "required": ["mode"],
        },
    },
    {
        "name": "sandbox_file_search",
        "description": "Search file contents (ripgrep) or find files by name (glob).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "search_type": {
                    "type": "string",
                    "enum": ["content", "files"],
                    "description": "default: content",
                },
                "machine": {
                    "type": "string",
                    "description": "Machine name (default: default machine)",
                },
                "path": {"type": "string", "description": "Directory to search (default: cwd)"},
                "file_glob": {"type": "string", "description": "Filter files (e.g. *.py)"},
                "limit": {"type": "integer", "description": "Max results (default: 50)"},
                "offset": {"type": "integer", "description": "Skip first N (default: 0)"},
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_only", "count"],
                    "description": "default: content",
                },
                "context": {"type": "integer", "description": "Context lines (default: 0)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "sandbox_env",
        "description": (
            "Environment management. Call action=help to discover "
            "operations or action=status for current state. "
            "Other actions are discovered on demand."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Operation name. Start with help or status.",
                },
                "params": {
                    "type": "object",
                    "description": "Operation params documented by help actions.",
                },
            },
            "required": ["action"],
        },
    },
]


_AUDIT_QUERY_TOOL_DEFINITION = {
    "name": "sandbox_audit_query",
    "description": (
        "Query the audit log (read-only). Reads at most `tail` lines from "
        "the end of the file; filters apply within that tail; `start`/`end` "
        "page over the filtered results."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "tail": {"type": "integer", "default": 5000, "minimum": 1, "maximum": 100000},
            "start": {"type": "integer", "default": 0, "minimum": 0},
            "end": {"type": "integer", "default": 100, "minimum": 1},
            "action": {"type": "string"},
            "machine": {"type": "string"},
            "status": {"type": "string"},
            "since": {"type": "number"},
            "until": {"type": "number"},
        },
    },
}


class ToolDef:
    def __init__(self, name, description, inputSchema):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class TextContent:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class SandboxServer:
    """Core sandbox MCP server logic (transport-agnostic)."""

    def __init__(self, audit: AuditLogger | None = None):
        self.machines = TargetRegistry()
        self.shells = ShellRegistry()
        self._docker_backend = DockerBackend()
        self._ssh_backend = SSHBackend()
        self.sandbox_env = SandboxEnv(
            self.machines, self.shells, self._docker_backend, self._ssh_backend
        )
        self.audit = audit if audit is not None else DEFAULT_AUDIT_LOGGER
        # Bootstrap: run ``docker_ps`` once before any request can be
        # served so pre-existing labeled containers are adopted into
        # the registry.  Failures are logged and tolerated so a
        # transient docker outage doesn't prevent boot.
        try:
            self.sandbox_env.dispatch("docker_ps", {})
        except Exception:
            logger.exception("startup bootstrap: docker_ps failed")
        # Provision a default machine when opted in ([default_machine]
        # enabled).  Provisioning failure is fatal: the operator asked
        # for a guaranteed default, so surprising the agent at first use
        # is worse than refusing to start.  Runs after docker_ps so a
        # surviving default container is re-adopted, not re-created.
        self._provision_default_machine()

    def _provision_default_machine(self) -> None:
        """Create the configured default machine at startup (opt-in).

        See :class:`~sandbox_mcp.config.DefaultMachineConfig`.  When
        ``enabled`` is false this is a no-op (the historical lazy
        behaviour).  When enabled, a provisioning failure raises
        ``RuntimeError`` and the server refuses to start.
        """
        cfg = _load_config().default_machine
        if not cfg.enabled:
            return
        name = cfg.name
        if name in self.machines.list_machines():
            # docker_ps reconciliation already re-adopted it (e.g. the
            # container survived a server restart).  Just make sure it's
            # the default.
            self.machines.set_default(name)
            logger.info("default machine %r already registered (reattached)", name)
            return

        logger.info("provisioning default machine %r via %s backend", name, cfg.backend)
        try:
            if cfg.backend == "docker":
                # No image override here: the default machine reuses
                # [docker] default_image (backend-specific config lives
                # in its own section, not under [default_machine]).
                info = self.machines.register(
                    name,
                    self._docker_backend,
                    purpose=cfg.purpose,
                )
            elif cfg.backend == "ssh":
                ssh_cfg = _load_config().ssh
                if not ssh_cfg.default_host or not ssh_cfg.default_user:
                    raise RuntimeError(
                        "[default_machine] backend='ssh' requires "
                        "[ssh] default_host and default_user"
                    )
                info = self.machines.register(
                    name,
                    self._ssh_backend,
                    purpose=cfg.purpose,
                    host=ssh_cfg.default_host,
                    user=ssh_cfg.default_user,
                    port=ssh_cfg.default_port,
                    key=ssh_cfg.default_key or None,
                )
            else:
                raise RuntimeError(
                    f"[default_machine] unknown backend: {cfg.backend!r} "
                    "(expected 'docker' or 'ssh')"
                )
        except Exception as e:
            raise RuntimeError(
                f"failed to provision default machine {name!r} via {cfg.backend}: {e}"
            ) from e

        if info.status != "running":
            raise RuntimeError(
                f"failed to provision default machine {name!r} via {cfg.backend}: "
                f"{getattr(info, 'error', None) or info.status}"
            )
        self.machines.set_default(name)
        detail = f" ({cfg.backend} backend)"
        if info.note:
            detail += f": {info.note}"
        logger.info("default machine %r ready%s", name, detail)

    def list_tools(self):
        tools = [ToolDef(t["name"], t["description"], t["inputSchema"]) for t in TOOL_DEFINITIONS]
        if _load_config().audit.log_path:
            t = _AUDIT_QUERY_TOOL_DEFINITION
            tools.append(ToolDef(t["name"], t["description"], t["inputSchema"]))
        return tools

    def call_tool(self, name, arguments):
        handler = getattr(self, f"_handle_{name}", None)
        if handler is None:
            return [TextContent(json.dumps({"error": f"Unknown tool: {name}"}))]
        arguments = arguments or {}
        start = time.monotonic()
        status = "ok"
        try:
            result = handler(arguments)
            return [TextContent(json.dumps(result, ensure_ascii=False))]
        except Exception as e:
            status = "error"
            logger.exception("call_tool %s failed", name)
            return [
                TextContent(
                    json.dumps(
                        {
                            "error": str(e),
                            "type": type(e).__name__,
                        }
                    )
                )
            ]
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            # Querying the audit log shouldn't pollute it.
            if name != "sandbox_audit_query":
                arguments = arguments or {}
                # ``sandbox_env`` is a meta-tool: the real action lives
                # in ``arguments["action"]``.  For every other tool the
                # tool name IS the action.  ``machine`` is the only
                # argument promoted to a top-level indexed column.
                if name == "sandbox_env":
                    action = arguments.get("action", name)
                    details = {k: v for k, v in arguments.items() if k != "action"}
                else:
                    action = name
                    details = {k: v for k, v in arguments.items() if k != "machine"}
                machine = arguments.get("machine")
                self.audit.record(
                    machine=machine,
                    action=action,
                    status=status,
                    duration_ms=duration_ms,
                    details=details,
                )

    def _resolve_machine(self, arguments):
        return self.machines.resolve_machine(arguments.get("machine"))

    # ---- shell handlers ----

    def _handle_sandbox_shell_exec(self, args):
        timeout = args.get("timeout", 30)
        wait = args.get("wait", True)
        max_output = args.get("max_output", 50000)
        shell_id = args.get("shell_id")

        if shell_id:
            session = self.shells.get(shell_id)
            if session is None:
                return {"error": f"Unknown shell_id: {shell_id}"}
        else:
            machine = self._resolve_machine(args)
            backend = self.machines.get_backend(machine)
            sid = self.shells.get_or_create_default(machine, lambda: backend.open_shell(machine))
            session = self.shells.get(sid)

        return session.send(args["command"], wait=wait, timeout=timeout, max_output=max_output)

    def _handle_sandbox_shell_read(self, args):
        session = self.shells.get(args["shell_id"])
        if session is None:
            return {"error": f"Unknown shell_id: {args['shell_id']}"}
        return session.read()

    # ---- file handlers ----

    def _get_file_ops(self, machine):
        backend = self.machines.get_backend(machine)
        return FileOperations(backend)

    def _handle_sandbox_file_read(self, args):
        machine = self._resolve_machine(args)
        fops = self._get_file_ops(machine)
        return fops.read(
            args["path"],
            machine,
            offset=args.get("offset", 1),
            limit=min(args.get("limit", 500), 2000),
        )

    def _handle_sandbox_file_write(self, args):
        machine = self._resolve_machine(args)
        fops = self._get_file_ops(machine)
        return fops.write(args["path"], args["content"], machine)

    def _handle_sandbox_file_patch(self, args):
        machine = self._resolve_machine(args)
        fops = self._get_file_ops(machine)
        return fops.patch(
            mode=args["mode"],
            machine=machine,
            path=args.get("path", ""),
            old_string=args.get("old_string", ""),
            new_string=args.get("new_string", ""),
            replace_all=args.get("replace_all", False),
            patch=args.get("patch", ""),
        )

    def _handle_sandbox_file_search(self, args):
        machine = self._resolve_machine(args)
        fops = self._get_file_ops(machine)
        return fops.search(
            pattern=args["pattern"],
            machine=machine,
            search_type=args.get("search_type", "content"),
            path=args.get("path", "."),
            file_glob=args.get("file_glob", ""),
            limit=args.get("limit", 50),
            offset=args.get("offset", 0),
            output_mode=args.get("output_mode", "content"),
            context=args.get("context", 0),
        )

    # ---- audit_query handler ----

    def _handle_sandbox_audit_query(self, args):
        cfg = _load_config()
        log_path = cfg.audit.log_path
        if not log_path:
            return {"error": "audit log is not file-backed"}

        start = int(args.get("start", 0))
        raw_end = args.get("end")
        end = int(raw_end) if raw_end is not None else None

        path = Path(log_path).expanduser()
        result = query_audit(
            path,
            tail=int(args.get("tail", DEFAULT_TAIL)),
            start=start,
            end=end,
            action=args.get("action"),
            machine=args.get("machine"),
            status=args.get("status"),
            since=args.get("since"),
            until=args.get("until"),
        )
        total = result["total"]
        # `end` may have been defaulted inside ``query_audit`` (start + 100);
        # re-derive the effective end so the window reflects the call.
        effective_end = end if end is not None else start + 100
        window_end = min(effective_end, total)
        window_start = min(start, total)
        return {
            "records": result["records"],
            "total": total,
            "tail_size": result["tail_size"],
            "window": [window_start, window_end],
        }

    # ---- sandbox_env handler ----

    def _handle_sandbox_env(self, args):
        action = args.get("action", "")
        params = args.get("params", {})
        return self.sandbox_env.dispatch(action, params)


def main(argv: list[str] | None = None):
    """Entry point: run the MCP server over stdio."""
    import asyncio

    args = _build_arg_parser(
        prog="sandbox-mcp",
        with_http=False,
        description="Run sandbox-mcp over stdio (for Hermes and other MCP hosts).",
    ).parse_args(argv)
    _apply_cli_overrides_to_env(args)

    try:
        import mcp.types as types
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError:
        logging.error("mcp package not installed. Run: pip install mcp")
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    audit_log_path = _load_config().audit.log_path
    if audit_log_path:
        logger.info("audit: log_path=%s (query tool enabled)", audit_log_path)
    else:
        logger.warning("audit: log_path=<empty> (query tool disabled)")
    server = SandboxServer()
    mcp_server = Server("sandbox-mcp")

    @mcp_server.list_tools()
    async def handle_list_tools():
        return [
            types.Tool(name=t.name, description=t.description, inputSchema=t.inputSchema)
            for t in server.list_tools()
        ]

    @mcp_server.call_tool()
    async def handle_call_tool(name, arguments):
        result = server.call_tool(name, arguments)
        return [types.TextContent(type=item.type, text=item.text) for item in result]

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.run(
                read_stream, write_stream, mcp_server.create_initialization_options()
            )

    asyncio.run(run())


def main_http(argv: list[str] | None = None):
    """Entry point: run the MCP server as an HTTP service.

    Bind address comes from CLI ``--host`` / ``--port``, then
    ``[server]`` in the config file, then the built-in default.

    The MCP server is mounted on ``/mcp`` using the Streamable HTTP
    transport (current MCP spec).

    HTTP requests are gated by ``BearerAuthMiddleware``.  Tokens are
    read from a file on disk (env > config > ``~/.sandbox-mcp/auth_tokens``).
    Tokens are re-read from the file on every request, so changes to the
    file take effect immediately (hot-reload, like sshd's authorized_keys).
    If the file is missing/empty and ``auto_generate_if_empty`` is set,
    an ephemeral token is generated and printed to stderr; otherwise the
    server refuses to start (fail-closed).
    """
    import sys

    from sandbox_mcp.auth import (
        generate_ephemeral_token,
        load_auth_tokens,
        resolve_tokens_file,
    )

    args = _build_arg_parser(
        prog="sandbox-mcp-http",
        with_http=True,
        description="Run sandbox-mcp as a standalone HTTP MCP server.",
    ).parse_args(argv)
    _apply_cli_overrides_to_env(args)

    server_cfg = _load_config().server
    host = server_cfg.host
    port = server_cfg.port
    reset_default_logger()  # honour [audit] log_path

    # --- Resolve tokens ---------------------------------------------------
    tokens_file = resolve_tokens_file()
    tokens = load_auth_tokens(tokens_file)
    if not tokens:
        if server_cfg.auto_generate_if_empty:
            ephemeral = generate_ephemeral_token()
            print(
                "\n[sandbox-mcp-http] WARNING: no tokens found at "
                f"{tokens_file}.\n"
                "Generated ephemeral token (capture now, will not be shown again):\n"
                f"  {ephemeral}\n"
                "Pass it as: Authorization: Bearer <token>\n",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                "[sandbox-mcp-http] FATAL: no auth tokens configured.\n"
                f"  Create {tokens_file} with one bearer token per line, then\n"
                "  chmod 600 it.  Or set:\n"
                "    [server] auto_generate_if_empty = true  (ephemeral dev token)\n"
                "  or\n"
                "    $SANDBOX_MCP_SERVER_AUTH_TOKENS_FILE  (custom path)\n",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    audit_log_path = _load_config().audit.log_path
    if audit_log_path:
        logger.info("audit: log_path=%s (query tool enabled)", audit_log_path)
    else:
        logger.warning("audit: log_path=<empty> (query tool disabled)")
    logger.info("Starting sandbox-mcp HTTP server on %s:%s", host, port)
    logger.info("Tokens file: %s (hot-reloaded per request)", tokens_file)

    app = _build_http_app(tokens_file=tokens_file)

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


def _build_http_app(*, tokens_file):
    """Build the Starlette ASGI app for the HTTP transport.

    Mounts a single ``/mcp`` endpoint backed by
    :class:`StreamableHTTPSessionManager` (current MCP spec).

    The app is wrapped in :class:`BearerAuthMiddleware` which re-reads
    the token file on every request for hot-reload.
    """
    import mcp.types as types
    from mcp.server import Server
    from starlette.applications import Starlette
    from starlette.routing import Mount

    server = SandboxServer()
    mcp_server = Server("sandbox-mcp")

    @mcp_server.list_tools()
    async def handle_list_tools():
        return [
            types.Tool(name=t.name, description=t.description, inputSchema=t.inputSchema)
            for t in server.list_tools()
        ]

    @mcp_server.call_tool()
    async def handle_call_tool(name, arguments):
        result = server.call_tool(name, arguments)
        return [types.TextContent(type=item.type, text=item.text) for item in result]

    from contextlib import asynccontextmanager

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        json_response=False,
        stateless=False,
    )

    @asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            yield

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    app = Starlette(
        routes=[Mount("/mcp", app=handle_mcp)],
        lifespan=lifespan,
    )

    # Wrap with bearer-token auth.  Middleware re-reads the token file
    # on every request so token changes take effect immediately.
    app.add_middleware(BearerAuthMiddleware, tokens_file=tokens_file)
    return app


class BearerAuthMiddleware:
    """Starlette ASGI middleware that requires ``Authorization: Bearer <t>``.

    Tokens are re-read from the file on every request (hot-reload, like
    sshd's authorized_keys).  Tokens are compared in constant time via
    ``secrets.compare_digest`` to defeat timing side-channels.  Failures
    respond ``401 Unauthorized`` with a ``WWW-Authenticate: Bearer``
    challenge so MCP clients know how to retry.
    """

    def __init__(self, app, tokens_file) -> None:
        self.app = app
        self.tokens_file = tokens_file

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Find Authorization header (case-insensitive lookup per RFC 7230).
        headers = {
            k.decode("ascii").lower(): v.decode("ascii", errors="replace")
            for k, v in scope.get("headers", [])
        }
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            await _send_401(send, "missing or malformed Authorization header")
            return
        presented = auth[len("Bearer ") :].strip()
        if not presented:
            await _send_401(send, "empty bearer token")
            return

        # Re-read tokens from file on every request (hot-reload).
        from sandbox_mcp.auth import load_auth_tokens

        tokens = load_auth_tokens(self.tokens_file)
        if not any(secrets.compare_digest(presented, t) for t in tokens):
            await _send_401(send, "invalid token")
            return

        await self.app(scope, receive, send)


async def _send_401(send, reason: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"www-authenticate", b'Bearer realm="sandbox-mcp"'),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": f"401 unauthorized: {reason}\n".encode(),
        }
    )


if __name__ == "__main__":
    main()
