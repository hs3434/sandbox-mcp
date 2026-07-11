"""Sandbox MCP Server: 7 tools (6 core + 1 sandbox_env entry).

Tools exposed via MCP tools/list:
  - sandbox_shell_exec
  - sandbox_shell_read
  - sandbox_file_read
  - sandbox_file_write
  - sandbox_file_patch
  - sandbox_file_search
  - sandbox_env  (progressive discovery)
"""

from __future__ import annotations

import json
import logging

from sandbox_mcp.backends.docker_backend import DockerBackend
from sandbox_mcp.backends.ssh_backend import SSHBackend
from sandbox_mcp.file_operations import FileOperations
from sandbox_mcp.sandbox_env import SandboxEnv
from sandbox_mcp.shell_registry import ShellRegistry
from sandbox_mcp.target_registry import TargetRegistry

logger = logging.getLogger(__name__)


TOOL_DEFINITIONS = [
    {
        "name": "sandbox_shell_exec",
        "description": ("Execute a shell command. wait=true (default) blocks until "
                        "completion or timeout. wait=false returns after "
                        "confirming execution started."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "description": "Shell command to execute"},
                "shell_id": {"type": "string",
                             "description": "Specific shell (default: machine's default shell)"},
                "machine": {"type": "string",
                            "description": "Machine name (default: default machine)"},
                "wait": {"type": "boolean",
                         "description": "Wait for completion (default: true)"},
                "timeout": {"type": "integer",
                            "description": "Seconds to wait (default: 30)"},
                "max_output": {"type": "integer",
                               "description": "Max output bytes (default: 50000)"},
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
                "shell_id": {"type": "string",
                             "description": "Shell to read from"},
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
                "machine": {"type": "string",
                            "description": "Machine name (default: default machine)"},
                "offset": {"type": "integer",
                           "description": "Start line (1-indexed, default: 1)"},
                "limit": {"type": "integer",
                          "description": "Max lines (default: 500, max: 2000)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "sandbox_file_write",
        "description": ("Write content to a file, replacing existing. "
                        "Creates parent dirs. Runs syntax check for known "
                        "extensions."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string",
                            "description": "Complete file content"},
                "machine": {"type": "string",
                            "description": "Machine name (default: default machine)"},
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
                "path": {"type": "string",
                         "description": "File path (replace mode)"},
                "old_string": {"type": "string",
                               "description": "Text to find (replace mode)"},
                "new_string": {"type": "string",
                               "description": "Replacement text (replace mode)"},
                "replace_all": {"type": "boolean",
                                "description": "Replace all (default: false)"},
                "patch": {"type": "string",
                          "description": "Patch content (patch mode)"},
                "machine": {"type": "string",
                            "description": "Machine name (default: default machine)"},
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
                "search_type": {"type": "string", "enum": ["content", "files"],
                                "description": "default: content"},
                "machine": {"type": "string",
                            "description": "Machine name (default: default machine)"},
                "path": {"type": "string",
                         "description": "Directory to search (default: cwd)"},
                "file_glob": {"type": "string",
                              "description": "Filter files (e.g. *.py)"},
                "limit": {"type": "integer",
                          "description": "Max results (default: 50)"},
                "offset": {"type": "integer",
                           "description": "Skip first N (default: 0)"},
                "output_mode": {"type": "string",
                                "enum": ["content", "files_only", "count"],
                                "description": "default: content"},
                "context": {"type": "integer",
                            "description": "Context lines (default: 0)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "sandbox_env",
        "description": ("Environment management. Call action=help to discover "
                        "operations or action=status for current state. "
                        "Other actions are discovered on demand."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "Operation name. Start with help or status."},
                "params": {"type": "object",
                           "description": "Operation params documented by help actions."},
            },
            "required": ["action"],
        },
    },
]


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

    def __init__(self):
        self.machines = TargetRegistry()
        self.shells = ShellRegistry()
        self._docker_backend = DockerBackend()
        self._ssh_backend = SSHBackend()
        self.sandbox_env = SandboxEnv(self.machines, self.shells,
                                      self._docker_backend, self._ssh_backend)

    def list_tools(self):
        return [ToolDef(t["name"], t["description"], t["inputSchema"])
                for t in TOOL_DEFINITIONS]

    def call_tool(self, name, arguments):
        handler = getattr(self, f"_handle_{name}", None)
        if handler is None:
            return [TextContent(json.dumps({"error": f"Unknown tool: {name}"}))]
        try:
            result = handler(arguments or {})
            return [TextContent(json.dumps(result, ensure_ascii=False))]
        except Exception as e:
            return [TextContent(json.dumps({"error": str(e)}))]

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
            sid = self.shells.get_or_create_default(
                machine, lambda: backend.open_shell(machine))
            session = self.shells.get(sid)

        return session.send(args["command"], wait=wait, timeout=timeout,
                            max_output=max_output)

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
        return fops.read(args["path"], machine,
                         offset=args.get("offset", 1),
                         limit=min(args.get("limit", 500), 2000))

    def _handle_sandbox_file_write(self, args):
        machine = self._resolve_machine(args)
        fops = self._get_file_ops(machine)
        return fops.write(args["path"], args["content"], machine)

    def _handle_sandbox_file_patch(self, args):
        machine = self._resolve_machine(args)
        fops = self._get_file_ops(machine)
        return fops.patch(
            mode=args["mode"], machine=machine,
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
            pattern=args["pattern"], machine=machine,
            search_type=args.get("search_type", "content"),
            path=args.get("path", "."),
            file_glob=args.get("file_glob", ""),
            limit=args.get("limit", 50),
            offset=args.get("offset", 0),
            output_mode=args.get("output_mode", "content"),
            context=args.get("context", 0),
        )

    # ---- sandbox_env handler ----

    def _handle_sandbox_env(self, args):
        action = args.get("action", "")
        params = args.get("params", {})
        return self.sandbox_env.dispatch(action, params)


def main():
    """Entry point: run the MCP server over stdio."""
    import asyncio
    try:
        import mcp.types as types
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError:
        logging.error("mcp package not installed. Run: pip install mcp")
        return

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    server = SandboxServer()
    mcp_server = Server("sandbox-mcp")

    @mcp_server.list_tools()
    async def handle_list_tools():
        return [
            types.Tool(name=t.name, description=t.description,
                       inputSchema=t.inputSchema)
            for t in server.list_tools()
        ]

    @mcp_server.call_tool()
    async def handle_call_tool(name, arguments):
        return server.call_tool(name, arguments)

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.run(read_stream, write_stream,
                                 mcp_server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
