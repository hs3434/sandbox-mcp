# Sandbox Environment Manager MCP

An MCP (Model Context Protocol) server that provides persistent sandbox environment
management for AI agents. Manages Docker containers and SSH machines as execution
machines, with shell-based command execution and full file operation capabilities.

Designed as a replacement for Hermes Agent's built-in terminal/file/code_execution
tools, adding persistent environment management that the built-in tools lack.

## Features

- **Compact MCP surface**: 7 exposed tools, with progressive management discovery via `sandbox_env`
- **Multi-backend**: Docker containers + SSH remote machines
- **Persistent machines**: Environments survive across sessions
- **Shell-based execution**: command execution with dual-marker confirmation, read for long-running commands
- **Full file operations**: read, write, patch (fuzzy match), search (ripgrep/glob)
- **Docker image building**: Agent can define custom environments
- **Docker commit**: Save container state for persistence

## Quick Start

```bash
# Install (with dev extras for tests)
pip install -e ".[dev]"

# Run unit tests
pytest tests/ --ignore=tests/test_integration_docker.py -v

# Register with Hermes (add to ~/.hermes/config.yaml)
# mcp_servers:
#   sandbox:
#     command: sandbox-mcp

# Disable built-in tools (optional, to avoid duplicate schemas)
# agent:
#   disabled_toolsets:
#     - terminal
#     - file
#     - code_execution
```

## Tools

| Tool | Purpose |
|------|---------|
| `sandbox_shell_exec` | Execute a shell command (wait or non-blocking) |
| `sandbox_shell_read` | Read new output from a shell |
| `sandbox_file_read` | Read a text file with line numbers |
| `sandbox_file_write` | Write a file (auto mkdir, syntax check) |
| `sandbox_file_patch` | Targeted edit with fuzzy match |
| `sandbox_file_search` | Ripgrep content search + glob file search |
| `sandbox_env` | Progressive discovery: `default_set`, `shell_*`, `docker_*`, `ssh_*` |

## Limitations

- **SSH backend uses key auth only.** Password authentication is not supported in the initial release.
- **No recovery across MCP server restarts.** machines and shell sessions are lost on server restart; re-create them with `docker_run` or `ssh_connect`.
- **No PTY / interactive stdin.** Commands run non-interactively. Commands that expect a TTY (vim, ssh password prompts) are not supported.

## Design

See [docs/design-spec-v2.md](docs/design-spec-v2.md) for the current design specification.
