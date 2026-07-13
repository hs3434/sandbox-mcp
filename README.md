# Sandbox Environment Manager MCP

**Languages**: [English](README.md) · [中文](README.zh.md)

An MCP (Model Context Protocol) server that provides persistent sandbox environment
management for AI agents. Manages Docker containers and SSH machines as execution
targets, with shell-based command execution and full file operation capabilities.

Designed as a replacement for Hermes Agent's built-in terminal/file/code_execution
tools, adding persistent environment management that the built-in tools lack.

## Features

- **Compact MCP surface**: 7 exposed tools, with progressive management discovery via `sandbox_env`
- **Dual transport**: stdio (Hermes child process) or HTTP (independent service)
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

- **`sandbox-mcp-http`** — standalone HTTP service.  Start it from a shell:
  ```bash
  sandbox-mcp-http
  # Then connect any MCP client to http://127.0.0.1:8010/mcp
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
# standalone HTTP server (default: streamable-http on /mcp)
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

To customize, copy [`config/config.example.toml`](config/config.example.toml) from the
repo root to `~/.sandbox-mcp/config.toml` and edit what you need.
Leaving it in place means all defaults are used.

Config sections:

```toml
[server]                # HTTP server
host = "0.0.0.0"
port = 8010
transport = "streamable-http"

[storage]               # persistent workspace directory
work_home = "~/.sandbox-mcp/workspaces/"

[audit]                 # SQLite audit log (one row per tool call)
log_path = "~/.sandbox-mcp/audit.db"
                        # "" = stderr (sandbox_audit_query hidden); file = query tool enabled

[docker]                # container defaults
container_name_prefix = "sandbox-"
default_image = "debian:stable-slim"
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
SANDBOX_MCP_AUDIT_LOG_PATH=/var/log/sandbox-mcp/audit.db sandbox-mcp
```

The `work_home` directory is created automatically. When `docker_run` is called,
a subdirectory `work_home/<machine-name>/` is created and bind-mounted to
`/workspace` inside the container — the agent works in `/workspace` without
ever seeing a host path.

### Register with Hermes

**Stdio transport** (the `sandbox-mcp` command):

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  sandbox:
    command: sandbox-mcp
    # Optional: pass CLI flags to the server.
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

**HTTP transport** (the `sandbox-mcp-http` command):

```yaml
mcp_servers:
  sandbox:
    url: "http://localhost:8010/mcp"
    headers:
      Authorization: "Bearer <your-token>"

agent:
  disabled_toolsets:
    - terminal
    - file
    - code_execution
```

Hermes connects to the HTTP MCP endpoint (`/mcp`, the current MCP spec
"Streamable HTTP" transport).  Useful when the MCP server runs on a
different machine or is managed as a systemd service.

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
| `sandbox_audit_query` | Read the audit log (filtered, paginated) — only when `[audit] log_path` is set |

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

### Container networking

All containers created by `docker_run` join a shared user-defined bridge
network (`sandbox-mcp` by default).  This means containers can reach each
other by their container name (DNS-resolvable):

```python
sandbox_env(action="docker_run", name="db", image="postgres:16")
sandbox_env(action="docker_run", name="dev", image="debian:stable-slim")
# Inside "dev" container: psql -h sandbox-db
#                              ^ DNS resolves to the "db" container's IP

sandbox_env(action="docker_run", name="web", image="nginx:latest")
# Inside "dev" container: curl http://sandbox-web
#                              ^ DNS resolves to the "web" container's IP
```

The network name is configured via `[docker] auto_network` (default
`"sandbox-mcp"`).  Set it to an empty string to opt out:

```toml
[docker]
auto_network = ""
```

The network is created lazily on the first `docker_run` call, so no
startup dependency exists.

### `docker_build` Usage

The agent never touches the host filesystem. `docker_build` only
accepts file mode:

```python
sandbox_file_write(path="/workspace/Dockerfile",
                   content="FROM debian:stable-slim\nRUN apt install -y python3\n")
sandbox_env(action="docker_build",
            machine="dev",
            image_tag="myapp:v1")
# Defaults: dockerfile=/workspace/Dockerfile, context_dir=/workspace
# sandbox-mcp translates the container path to work_home/<machine>/ on the host
```

**Sandbox boundary**: `dockerfile` and `context_dir` must live under
`/workspace/`. Host paths are rejected — the agent cannot reach files
outside its assigned `work_home/<machine>/`.

> **Why no inline `dockerfile_content`?** An inline Dockerfile would
> skip the sandbox's file-write audit trail AND be fed verbatim to the
> docker daemon, whose build steps execute with full host kernel
> capabilities (e.g. BuildKit `--mount=type=bind,source=/,...`). The
> agent has to commit its Dockerfile to disk via `sandbox_file_write`
> first, which keeps every line auditable and the build context under
> `work_home`.

### `docker_run` Sandbox Boundary

The agent cannot smuggle arbitrary host paths into a sandboxed
container:

- `volumes=[]` is **not accepted**. The only bind mount is the
  auto-attached `work_home/<machine>` → `/workspace`. Attempts to pass
  `volumes=["/:/host", "/etc:/host-etc"]` are silently dropped.
- The agent can run any image and `docker exec` any command *inside*
  the container, but cannot mount host paths, cannot read host
  `/etc`, `/root`, etc. from inside.

This is a deliberate **first line of defense**: the sandbox's
file-write boundary extends into `docker_run`. **Caveats** still apply:
the container shares the host kernel, so kernel-capability exploits
(`unshare`, kernel CVEs) are not stopped by this. For stronger
isolation, deploy with rootless docker or gVisor (`runsc`).

### Connecting to a Remote Docker Daemon

By default `sandbox-mcp` talks to the docker daemon at
`unix:///var/run/docker.sock` (or wherever `$DOCKER_HOST` points).  To
point at a remote daemon, set `[docker] host` in `config.toml` (env
override: `SANDBOX_MCP_DOCKER_HOST`):

```toml
# Remote daemon over TLS (recommended for non-local daemons).
[docker]
host = "tcp://docker.internal:2376"
tls_verify = true
cert_path = "/etc/sandbox-mcp/docker-certs"

# Or ride your existing SSH trust — uses paramiko, no cert needed.
# host = "ssh://deploy@docker-prod.internal"

# Or a custom socket path when bind-mounted into a container.
# host = "unix:///var/run/docker.sock"
```

URL scheme (`unix://` / `tcp://` / `ssh://`) selects transport.  See
[`config/config.example.toml`](config/config.example.toml) for all options.

## HTTP authentication

The HTTP transport (`sandbox-mcp-http`) requires a bearer token
on every request.  Tokens are stored in a file, one per line:

```
~/.sandbox-mcp/auth_tokens           # default path
```

The file **must** be mode ``0600`` before sandbox-mcp will start.
World/group readable files are rejected (fail-closed):

```bash
chmod 600 ~/.sandbox-mcp/auth_tokens
```

The path can be changed via the config file:

```toml
[server]
auth_tokens_file = "/etc/sandbox-mcp/auth_tokens"
```

Or via an env var (overrides everything):

```bash
SANDBOX_MCP_SERVER_AUTH_TOKENS_FILE=/run/secrets/auth_tokens sandbox-mcp-http
```

When you connect an MCP client, include the token in the
``Authorization`` header:

```bash
# default streamable-http transport
curl -X POST -H "Authorization: Bearer <your-token>" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' \
     http://127.0.0.1:8010/mcp
```

### Auto-generating a dev token

Set ``auto_generate_if_empty = true`` in the config file or export
``SANDBOX_MCP_SERVER_AUTO_GENERATE_IF_EMPTY=true``.  If the token file
is missing or empty, an ephemeral token is generated at startup and
printed to stderr:

```
[sandbox-mcp-http] WARNING: no tokens found at ~/.sandbox-mcp/auth_tokens.
Generated ephemeral token (capture now, will not be shown again):
  XKTUv1Gjv2...33-chars-long
Pass it as: Authorization: Bearer <token>
```

Capture this token and use it for the session.  The server will not
regenerate it on restart without the file present.

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
  │  JSON-RPC over stdio │  or  │ HTTP (/mcp)
  ▼                              ▼
sandbox-mcp                     sandbox-mcp-http
  │  (stdio transport)           │  (streamable-http, port 8010)
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

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE)
(AGPL-3.0-only).

- **Open-source use** — you are free to use, modify, and distribute this software
  under the terms of the AGPLv3, including the requirement that modified versions
  serving users over a network must also provide their source code.
- **Commercial use** — if you wish to use this software in a closed-source or
  proprietary context without the AGPLv3 obligations, a separate commercial
  license is available. Contact **1606272735@qq.com** for details.