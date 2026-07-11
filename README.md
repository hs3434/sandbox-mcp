# Sandbox Environment Manager MCP

An MCP (Model Context Protocol) server that provides persistent sandbox environment
management for AI agents. Manages Docker containers and SSH machines as execution
targets, with shell-based command execution and full file operation capabilities.

Designed as a replacement for Hermes Agent's built-in terminal/file/code_execution
tools, adding persistent environment management that the built-in tools lack.

## Features

- **Compact MCP surface**: 7 exposed tools, with progressive management discovery via `sandbox_env`
- **Dual transport**: stdio (Hermes child process) or SSE/HTTP (independent service)
- **Multi-backend**: Docker containers (SDK, works with remote daemons) + SSH remote machines
- **Persistent machines**: Docker containers survive MCP restart; discover with `docker_ps`
- **Shell-based execution**: dual-marker confirmation, read for long-running commands
- **Full file operations**: read, write (atomic), patch (fuzzy match), search (ripgrep/glob)
- **In-process linters**: Python `ast`, JSON, optional YAML/TOML pre-write validation
- **Safety advisories**: non-blocking warnings for sensitive paths (`.ssh`, `.aws`, `.env*`)
- **Audit logging**: JSON-line stream of all tool invocations (content hashed)

## Quick Start

### Install

```bash
pip install .
pip install -e ".[dev]"   # + test/lint tools

# Run unit tests
pytest tests/ --ignore=tests/test_integration_docker.py -v
```

### Run

```bash
# stdio mode (default, for Hermes MCP)
sandbox-mcp

# HTTP/SSE mode (standalone service)
SANDBOX_MCP_HOST=0.0.0.0 SANDBOX_MCP_PORT=8010 sandbox-mcp-http
# Then connect any MCP client to http://host:8010/sse
```

### Register with Hermes (stdio)

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  sandbox:
    command: sandbox-mcp

# Disable built-in tools (optional, to avoid duplicate schemas)
agent:
  disabled_toolsets:
    - terminal
    - file
    - code_execution
```

## Tools

| Tool | Purpose |
|------|---------|
| `sandbox_shell_exec` | Execute a shell command (wait or non-blocking) |
| `sandbox_shell_read` | Read new output from a shell |
| `sandbox_file_read` | Read a text file with line numbers |
| `sandbox_file_write` | Write a file (auto mkdir, syntax check, atomic) |
| `sandbox_file_patch` | Targeted edit with fuzzy match |
| `sandbox_file_search` | Ripgrep content search + glob file search |
| `sandbox_env` | Progressive discovery: `default_set`, `shell_*`, `docker_*`, `ssh_*` |

## sandbox_env Actions

`sandbox_env` advertises only `help` and `status` by default. Call `action=help` to discover the full action set, or `action=docker_help` / `action=ssh_help` for backend-specific actions:

| namespace | actions |
|---|---|
| Discovery | `help`, `status` |
| General | `machine_list`, `default_set` |
| Shell | `shell_new`, `shell_list`, `shell_remove` |
| Docker | `docker_run`, `docker_build`, `docker_commit`, `docker_stop`, `docker_start`, `docker_remove`, `docker_ps`, `docker_images` |
| SSH | `ssh_connect`, `ssh_disconnect`, `ssh_reconnect`, `ssh_remove` |

`docker_run` is idempotent: if a container named `sandbox-<name>` already exists
(e.g. after an MCP restart), it reattaches instead of failing.

## Limitations

- **SSH backend uses key auth only.** Password authentication is not supported in the initial release.
- **No PTY / interactive stdin.** Commands run non-interactively. Commands that expect a TTY (vim, ssh password prompts) are not supported.
- **State is in-memory.** Shell sessions are lost on server restart; re-create with `shell_new`. Containers survive restart and can be reattached via `docker_run` or inspected via `docker_ps`.
- **No built-in session isolation.** Multiple agents connecting to the same server share the same machine/shell registry. This matches Hermes's own MCP behavior.

## Architecture Overview

```text
Agent (LLM)
  │
  ▼
MCP Client (Hermes Gateway | any MCP host)
  │  JSON-RPC over stdio │  or  │ SSE/HTTP
  ▼                              ▼
sandbox-mcp                     sandbox-mcp-http
  │  (stdio transport)           │  (SSE transport, port 8010)
  │                              │
  └──────────┬───────────────────┘
             │
             ▼
      Application Layer
  ┌──────────────────────┐
  │ 7 MCP tools          │
  │ sandbox_env dispatcher│
  │ ShellSession / ShellReg│
  │ MachineRegistry       │
  │ FileOperations        │
  │ AuditLogger / Safety  │
  └──────────┬───────────┘
             │
     ┌───────┴───────┐
     ▼               ▼
  Docker SDK      SSH (subprocess)
  (put_archive,    (ControlMaster,
   exec_run,        exec_oneoff,
   exec socket)     stdin pipe)
```

## Design

See [docs/design-spec-v2.md](docs/design-spec-v2.md) for the current design specification.
See [docs/implementation-plan.md](docs/implementation-plan.md) for the TDD implementation plan.