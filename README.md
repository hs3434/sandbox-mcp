# Sandbox Environment Manager MCP

An MCP (Model Context Protocol) server that provides persistent sandbox environment
management for AI agents. Manages Docker containers and SSH machines as execution
targets, with shell-based command execution and full file operation capabilities.

Designed as a replacement for Hermes Agent's built-in terminal/file/code_execution
tools, adding persistent environment management that the built-in tools lack.

## Features

- **Compact MCP surface**: 7 exposed tools, with progressive management discovery via `sandbox_env`
- **Multi-backend**: Docker containers + SSH remote machines
- **Persistent targets**: Environments survive across sessions
- **Shell-based execution**: command execution, real-time reads, stdin control, job management
- **Full file operations**: read, write, patch (fuzzy match), search (ripgrep/glob)
- **Docker image building**: Agent can define custom environments
- **Docker commit**: Save container state for persistence

## Quick Start

```bash
# Install
pip install -e .

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

## Design

See [docs/design-spec-v2.md](docs/design-spec-v2.md) for the current design specification.
