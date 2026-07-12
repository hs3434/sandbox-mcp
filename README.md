# Sandbox Environment Manager MCP

**Languages**: [English](README.md) · [中文](README.zh.md)

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

# Run unit tests (integration tests are skipped by default)
pytest tests/ -v

# Run integration tests (requires a running Docker daemon)
pytest tests/ -m integration -v
```

### Run

sandbox-mcp has two transports:

- **`sandbox-mcp-http`** — standalone HTTP/SSE service.  Start it from a shell:
  ```bash
  sandbox-mcp-http
  # Then connect any MCP client to http://127.0.0.1:8010/sse
  ```
- **`sandbox-mcp`** (stdio) — launched by an MCP host as a child process.
  Don't run this from a shell directly; configure it in the host (see
  [Register with Hermes](#register-with-hermes-stdio) below).

### CLI flags

| flag | applies to | purpose |
|---|---|---|
| `--config PATH` / `-c PATH` | both | path to a TOML config file |
| `--host ADDR` / `-H ADDR` | `sandbox-mcp-http` | HTTP bind address |
| `--port N` / `-p N` | `sandbox-mcp-http` | HTTP port |

```bash
# standalone HTTP/SSE server
sandbox-mcp-http -c /etc/sandbox-mcp/prod.toml --port 9000

# stdio (passed via the MCP host's config; not run from a shell)
#   see Register with Hermes below for an example
```

Precedence (highest first): **CLI flag** → env var → config file → built-in default.

### Configuration

sandbox-mcp reads config in this priority order (highest wins):

1. **CLI flags** (see above)
2. **Environment variables** — `SANDBOX_MCP_*` (e.g. `SANDBOX_MCP_SERVER_PORT`)
3. **Config file** — `~/.sandbox-mcp/config.toml` by default; overridden by
   `--config PATH` / `SANDBOX_MCP_CONFIG`
4. **Built-in defaults** (declared in `src/sandbox_mcp/config.py`)

To customize, copy [`config.example.toml`](config.example.toml) from the
repo root to `~/.sandbox-mcp/config.toml` and edit what you need.
Leaving it in place means all defaults are used.

Config sections:

```toml
[server]                # HTTP/SSE server
host = "0.0.0.0"
port = 8010

[storage]               # persistent workspace directory
work_home = "~/.sandbox-mcp/workspaces/"

[audit]                 # JSON-line audit log
log_path = ""           # "" = stderr; set to a file path to append

[docker]                # container defaults
container_name_prefix = "sandbox-"
default_image = "debian:stable-slim"
default_workdir = "/workspace"
restart_policy_name = "on-failure"
restart_max_retry_count = 3

[ssh]
connect_timeout = 10
socket_dir_prefix = "sandbox-mcp-ssh-"
tmpfile_pattern = ".sandbox-mcp-tmp.XXXXXX"

[shell]
default_max_output = 50000
head_size = 5120
tail_size = 46080

[files]
max_file_size = 51200
default_read_limit = 500
max_read_limit = 2000
default_search_limit = 50
```

Every value can also be overridden via env var (uppercased, dots → underscores), e.g.:

```bash
SANDBOX_MCP_SERVER_PORT=9000 sandbox-mcp-http
SANDBOX_MCP_DOCKER_CONTAINER_NAME_PREFIX="box-" sandbox-mcp
SANDBOX_MCP_AUDIT_LOG_PATH=/var/log/sandbox-mcp/audit.log sandbox-mcp
```

The `work_home` directory is created automatically. When `docker_run` is called,
a subdirectory `work_home/<machine-name>/` is created and bind-mounted to
`/workspace` inside the container — the agent works in `/workspace` without
ever seeing a host path.

### Register with Hermes (stdio)

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  sandbox:
    command: sandbox-mcp
    # Optional: pass CLI flags to the server.  The same flags work as on
    # the standalone server — sandbox-mcp reads its config from
    # --config / $SANDBOX_MCP_CONFIG / ~/.sandbox-mcp/config.toml.
    args:
      - --config
      - /etc/sandbox-mcp/prod.toml

# Disable built-in tools (optional, to avoid duplicate schemas)
agent:
  disabled_toolsets:
    - terminal
    - file
    - code_execution
```

Hermes spawns `sandbox-mcp` as a child process and pipes JSON-RPC over
its stdin/stdout.  The server has no GUI; it just waits for requests.

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

### `docker_build` Usage

The agent never touches the host filesystem. `docker_build` provides two
modes:

**File mode** (recommended): agent writes the Dockerfile into the container's
`/workspace/` via `sandbox_file_write`, then calls:

```python
sandbox_file_write(path="/workspace/Dockerfile",
                   content="FROM debian:stable-slim\nRUN apt install -y python3\n")
sandbox_env(action="docker_build",
            machine="dev",
            image_tag="myapp:v1")
# Defaults: dockerfile=/workspace/Dockerfile, context_dir=/workspace
# sandbox-mcp translates the container path to work_home/<machine>/ on the host
```

**Inline mode** (for one-shot builds or no running container):

```python
sandbox_env(action="docker_build",
            image_tag="myapp:latest",
            dockerfile_content="FROM debian:stable-slim\nRUN apt install -y python3\n")
# sandbox-mcp stages the content at work_home/_builds/<uuid>/Dockerfile and cleans up after
```

**Sandbox boundary**: `dockerfile` and `context_dir` must live under
`/workspace/`. Host paths are rejected — the agent cannot reach files
outside its assigned `work_home/<machine>/`.

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