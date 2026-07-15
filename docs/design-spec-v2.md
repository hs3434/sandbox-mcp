# Sandbox MCP v2 Design Spec

> Current design as of the 2026-07-10 design review.

## Problem (unchanged)

Hermes Agent's built-in terminal tool creates ephemeral containers that reset on
recreation. The agent cannot define persistent environments, deploy long-running
services, or manage processes reliably. The built-in file/terminal/code-execution
tools also consume context window space.

## What Changed from v1

| Aspect | v1 (19 tools) | v2 (7 tools) |
|--------|---------------|--------------|
| tools/list count | 19 | 7 (6 core + 1 sandbox_env) |
| Context per API call | ~2850 tokens | ~875 tokens |
| Management ops | All exposed directly | Progressive discovery via sandbox_env |
| Shell exec + write | Separate tools (exec + shell_write) | Merged into sandbox_shell_exec (wait param) |
| Shell creation/cleanup | Exposed tools | Discovered via sandbox_env(action="help") |
| Lifecycle ops | Generic stop/start/remove | Backend-specialized after docker_help/ssh_help |
| Shell I/O confirmation | Single marker (end only) | Dual marker (start + end) |
| Output buffer | Unbounded pipe reads | Drain thread, head 5K + tail ring buffer |
| Shell cleanup | Automatic | Manual via shell_remove |

## Architecture

```
Hermes Gateway (host process)
  └── MCP Client (JSON-RPC over stdio)
        └── Sandbox MCP Server (host process)
              ├── tools/list (7 tools, ~875 tokens/turn)
              │     ├── Core: sandbox_shell_exec, sandbox_shell_read
              │     ├── Core: sandbox_file_read/write/patch/search
              │     └── Entry: sandbox_env
              │           └── Default description only advertises help/status
              │
              └── sandbox_env progressive discovery
                    ├── action=help
                    │     ├── default_set
                    │     ├── shell_new / shell_list / shell_remove
                    │     └── docker_help / ssh_help
                    ├── action=docker_help
                    │     └── docker_run / docker_build / docker_commit
                    │         docker_stop / docker_start / docker_remove
                    │         docker_ps / docker_images
                    └── action=ssh_help
                          └── ssh_connect / ssh_disconnect / ssh_reconnect
                              ssh_remove
```

## Three-Layer Tool Exposure

### Layer 1: tools/list (always exposed, ~875 tokens)

7 tools with simple, well-defined schemas:

| Tool | Purpose | Frequency |
|------|---------|-----------|
| `sandbox_shell_exec` | Execute a shell command (wait or non-blocking) | High |
| `sandbox_shell_read` | Non-blocking read of shell output | High |
| `sandbox_file_read` | Read file with line numbers + pagination | High |
| `sandbox_file_write` | Write file (full content) | High |
| `sandbox_file_patch` | Targeted find-and-replace (fuzzy match) | High |
| `sandbox_file_search` | Ripgrep content search + glob file search | High |
| `sandbox_env` | Environment management discovery and dispatch | Low |

machine-aware core tools accept an optional `machine` parameter (default: default
machine set via `sandbox_env(action="default_set")`). `sandbox_shell_read` uses
`shell_id` and does not need a machine.

### Layer 2: sandbox_env help (on-demand, ~200 tokens)

The `sandbox_env` schema intentionally keeps the default `tools/list` entry
small. Its description only advertises two actions:

- `help`: discover common management operations
- `status`: inspect default machine, machines, and shells

`action` remains a free string, not an enum, so discovered operations can be
called through the same tool.

`sandbox_env(action="help")` returns:
- `default_set`: set default machine or default shell
- `shell_new`: create an additional shell session
- `shell_list`: list shell sessions
- `shell_remove`: terminate/remove a shell session
- Pointers to `docker_help` and `ssh_help`

### Layer 3: backend-specific help (on-demand, ~400 tokens each)

- `sandbox_env(action="docker_help")`: docker_run, docker_build, docker_commit,
  docker_stop, docker_start, docker_remove
- `sandbox_env(action="ssh_help")`: ssh_connect, ssh_disconnect, ssh_reconnect,
  ssh_remove

Agent only loads the backend help it needs. A Docker-only agent never loads
SSH docs.

### sandbox_env inputSchema (~100 tokens in tools/list)

```json
{
  "name": "sandbox_env",
  "description": "Environment management. Call action=help to discover management operations or action=status to inspect current state. Other actions are discovered on demand.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "action": {"type": "string", "description": "Operation name. Start with help or status."},
      "params": {"type": "object", "description": "Operation parameters, documented by help actions."}
    },
    "required": ["action"]
  }
}
```

### Context cost comparison

```
v1: 19 tools × ~150 tokens = ~2850 tokens/turn (every API call)
v2: 7 tools × ~125 tokens = ~875 tokens/turn (every API call)
    + help (200 tokens, once) + docker_help (400 tokens, once) = ~1475 total

Savings: ~50% on every API call, more for agents that don't need all backends.
```

## Shell Design

### sandbox_shell_exec

Replaces v1's `sandbox_exec` + `shell_write` with a single command execution
tool.

```
sandbox_shell_exec(command, shell_id?, machine?, wait=true, timeout=30, max_output=50000)
```

**Dual marker mechanism:**

```
Sent to shell stdin:
echo __START_<uuid>__
{command}
echo __END_<uuid>__:$?

stdout output stream:
__START_<uuid>__          ← confirms command started executing
...command output...       ← actual command output
__END_<uuid>__:0           ← confirms command finished, exit code = 0
```

**wait=true (blocking):**
1. Send command + dual markers
2. Wait for drain thread to signal __END_ found (or timeout)
3. Return immediately on __END_ (does NOT wait for full timeout)
4. On timeout: return partial output, status="running"

**wait=false (non-blocking):**
1. Send command + dual markers
2. Wait briefly for __START_ (~2s timeout)
3. Return with confirmed=true (command executing)
4. Agent uses shell_read() later for output

**Return values:**

```python
# wait=true
{"output": "...", "exit_code": 0, "status": "completed"}            # normal
{"output": "partial...", "exit_code": null, "status": "running"}     # timeout
{"output": "partial...", "exit_code": null, "status": "terminated"}  # bash died

# wait=false
{"status": "running", "confirmed": true}                             # started OK
{"status": "terminated", "confirmed": false}                         # bash died
```

**Why dual markers vs single marker:**

| | Single marker (v1) | Dual marker (v2) |
|---|---|---|
| Confirm execution started | No (guess from output) | Yes (__START_) |
| Return immediately on completion | Yes | Yes |
| wait=false confirmation | Impossible | __START_ brief wait |
| Timeout return includes confirmation | No | `confirmed: true/false` |
| Overhead | 1 echo | 2 echo (negligible) |

Critical for silent commands (sleep, pip install): single marker gives no feedback
until completion or timeout; dual marker's __START_ immediately confirms execution.

### Shell State Machine

```
States: idle | busy | running | terminated

sandbox_env(action="shell_new") -> idle

shell_exec(wait=true):
  idle -> busy (acquire lock, send command, wait for __END_)
  busy -> idle (__END_ found)
  busy -> running (timeout, lock released, command still running)

shell_exec(wait=false):
  idle -> running (send command, wait for __START_, release)

shell_read():
  running -> running (no __END_ yet)
  running -> idle (__END_ found by drain thread)
  idle -> idle (no output)
  any -> terminated (EOF detected by drain thread)

shell_remove():
  any -> removed from registry (no state, shell gone)

bash process exits/dies:
  any -> terminated (passive close, shell stays in registry)
```

**Concurrency rules:**

| Shell state | shell_exec | shell_read | shell_remove |
|-------------|-----------|------------|-------------|
| idle | Allowed | Returns empty | Allowed |
| busy | Rejected ("Shell is busy") | Rejected | Allowed (interrupts) |
| running | Rejected ("Shell is busy") | Allowed | Allowed |
| terminated | Rejected ("Shell terminated") | Returns remaining + terminated | Allowed (cleanup) |

Per-shell lock ensures atomic state transitions. shell_exec(wait=true) holds the
lock during blocking read; other operations on the same shell are rejected.

### Background Drain Thread

Each shell has a daemon drain thread that continuously reads from the bash
process's stdout pipe.

**Why needed:** Linux pipe buffer is 64KB. Without continuous draining, a
command producing >64KB output with no shell_read calls would block on write(),
causing deadlock.

**Buffer strategy: head 5KB + tail ring buffer (in-memory) + tail-only output on read**

```
drain thread ring buffer (in-memory, never read by shell_read):
├── head: first 5KB (fixed)
├── tail: last ~45KB ring buffer (old data discarded as new arrives)
└── total memory per shell: ~50KB
```

When `shell_read` returns output larger than `max_output` (default 50KB),
the implementation returns the **tail** (last `max_output` bytes) with a
truncation notice, not head+notice+tail. This is because command output
endings (errors, final results) are usually more useful than the opening.
The head buffer is kept in memory in case future revisions want to switch
to a head+tail scheme.

**Marker detection:** drain thread scans for __START_ and __END_ markers:
- __START_ found: signal shell_exec(wait=false) to return confirmed=true
- __END_ found: extract exit_code, set state idle (if running), signal
  shell_exec(wait=true) to return
- EOF (pipe closed): set state terminated, no exit_code available

**shell_read reads from the in-memory buffer**, never from the pipe directly.

### I/O Stream Handling

**Merged stdout+stderr** (`stderr=subprocess.STDOUT`), same as Hermes terminal tool.

Rationale:
- Matches real terminal behavior (stdout/stderr interleaved on screen)
- Preserves temporal ordering (important for understanding command behavior)
- Marker mechanism works cleanly with single stream
- shell_read is simpler (one stream)
- Agent can redirect if needed: `command 2>&1`, `command 2>/dev/null`

Hermes' `code_execution` tool separates streams (stdout=result, stderr=error)
because Python scripts have clear result/diagnostics separation. Shell commands
don't have this distinction.

### Output Truncation

```python
shell_exec(command, ..., max_output=50000)  # default 50KB
```

- Output <= max_output: return as-is
- Output > max_output: return tail (last max_output bytes) + truncation notice
  `[Output truncated: showing last 50KB of 500KB total]`
- Tail strategy (not head+tail) because command output endings are most
  important (errors, final results, summaries)

### shell_read Return Values

```python
{"output": "new data...", "status": "running"}                      # command still running
{"output": "final output...", "status": "completed", "exit_code": 0} # just completed
{"output": "", "status": "idle"}                                     # no command running
{"output": "remaining...", "status": "terminated"}                   # bash process died
```

shell_read detects command completion via drain thread's __END_ marker parsing.
Agent does not need to parse markers itself.

### Shell Cleanup

**No auto-cleanup.** Terminated shells stay in the registry until the agent
explicitly calls `shell_remove`. This prevents losing diagnostic information.

`shell_list` includes hints for terminated shells:

```json
[
  {"shell_id": "sh_abc", "machine": "dev", "status": "idle", "is_default": true, "uptime": "5m"},
  {"shell_id": "sh_def", "machine": "dev", "status": "terminated", "is_default": false,
   "hint": "Process exited. Call shell_remove to clean up."}
]
```

### write vs patch (kept separate)

Both are in core tools. Not merged because of token efficiency:

| | write | patch |
|---|---|---|
| Use case | Create/overwrite file | Targeted edit |
| Data sent | Full file content | old_string + new_string only |
| 500-line file, 1-line change | ~3000 tokens | ~30 tokens |
| Fuzzy matching | No | Yes (9 strategies) |
| Returns diff | No | Yes (unified diff) |

Hermes also keeps these as separate tools (`write_file` + `patch`).

## sandbox_env Progressive Discovery

`sandbox_env` is the only management tool exposed through MCP. It uses a
progressive discovery model to keep the default tool schema small:

1. `tools/list` only describes `action="help"` and `action="status"`.
2. `sandbox_env(action="help")` returns common management actions and pointers
   to backend-specific help.
3. `sandbox_env(action="docker_help")` or `sandbox_env(action="ssh_help")`
   returns backend-specific lifecycle actions.
4. Discovered actions are called through the same `sandbox_env(action, params)`
   interface.

### action="help" (static, ~200 tokens)

```json
{
  "default_actions": [
    {
      "action": "help",
      "description": "Discover common management actions and backend help entries"
    },
    {
      "action": "status",
      "description": "Inspect default machine, machines, and shell sessions"
    }
  ],
  "operations": [
    {
      "action": "default_set",
      "description": "Set default machine or default shell. Pass machine to set the default machine. Pass shell_id to set that shell as its machine's default shell.",
      "optional": {"machine": "string", "shell_id": "string"},
      "requires": "Exactly one of machine or shell_id",
      "example": {"machine": "dev", "shell_id": "sh_abc"}
    },
    {
      "action": "shell_new",
      "description": "Create an additional shell session on a machine.",
      "optional": {"machine": "string", "purpose": "string"}
    },
    {
      "action": "shell_list",
      "description": "List shell sessions, optionally filtered by machine.",
      "optional": {"machine": "string"}
    },
    {
      "action": "shell_remove",
      "description": "Terminate a live shell process and remove it from the registry. If already terminated, only remove the registry entry.",
      "required": {"shell_id": "string"}
    }
  ],
  "more_help": {
    "docker_help": "Discover Docker machine actions: run/build/commit/stop/start/remove",
    "ssh_help": "Discover SSH machine actions: connect/disconnect/reconnect/remove"
  },
  "note": "Core tools are directly exposed as sandbox_shell_exec, sandbox_shell_read, and sandbox_file_read/write/patch/search. machine-aware tools support optional machine."
}
```

### action="status" (dynamic)

```json
{
  "default_machine": "dev",
  "machines": [
    {"name": "dev", "backend": "docker", "status": "running",
     "purpose": "Python dev", "shells": 2, "uptime": "2h15m"}
  ],
  "shells": [
    {"shell_id": "sh_abc", "machine": "dev", "status": "idle", "purpose": "default", "is_default": true, "uptime": "5m"},
    {"shell_id": "sh_def", "machine": "dev", "status": "terminated", "is_default": false,
     "hint": "Process exited. Call shell_remove to clean up."}
  ]
}
```

### action="docker_help" (static, ~400 tokens)

Returns Docker operations with required/optional params, returns, and examples:

```
docker_run / docker_build / docker_commit
docker_stop / docker_start / docker_remove / docker_ps / docker_images
```

### action="ssh_help" (static, ~200 tokens)

Returns SSH operations with required/optional params, returns, and examples:

```
ssh_connect / ssh_disconnect / ssh_reconnect / ssh_remove
```

### Backend-specialized lifecycle operations

v1 had generic `stop`/`start`/`remove` that dispatched by backend. v2
specializes them:

| v1 (generic) | v2 Docker | v2 SSH | Semantic difference |
|---|---|---|---|
| stop | docker_stop | ssh_disconnect | Container stops vs connection closes |
| start | docker_start | ssh_reconnect | Container restarts vs connection re-establishes |
| remove | docker_remove | ssh_remove | Container destroyed vs machine unregistered |

Action name itself indicates the backend and behavior. No dispatch ambiguity.
Error messages can be specific: "docker_stop only works on Docker machines".

### Agent discovery flow

```
1. sandbox_env(action="help")            → default_set + shell actions + docker_help/ssh_help pointers
2. sandbox_env(action="status")          → current machines and shells
3. sandbox_env(action="docker_help")     → only if Docker needed (~400 tokens)
4. sandbox_env(action="docker_run", ...)  → create container
5. sandbox_env(action="default_set", ...) → set default machine
6. sandbox_shell_exec(command="...")      → work with core tools
   ...
7. sandbox_env(action="docker_stop", ...) → stop when done
```

## Complete sandbox_env Action List

```
Default discovery:  help / status
Common:             default_set
Shell:              shell_new / shell_list / shell_remove
Backend help:       docker_help / ssh_help
Docker:             docker_run / docker_build / docker_commit
                    docker_stop / docker_start / docker_remove
                    docker_ps / docker_images
SSH:                ssh_connect / ssh_disconnect / ssh_reconnect / ssh_remove
```

18 actions, 1 management tool in tools/list. Agent loads docs on demand.

## Backend Implementation

### Docker Backend

- Container naming: bare machine name, namespace enforced via the
  `sandbox-mcp.managed=true` label (deterministic, allows reconnection)
- Shell process: `docker exec -i <container> bash`
- Container lifecycle: `docker run -d --name <name> --init --restart
  on-failure:3 <image> sleep infinity`
- docker_stop: `docker stop <name>`
- docker_start: `docker start <name>`
- docker_remove: `docker rm -f <name>`
- docker_commit: `docker commit <name> <image_tag>`
- docker_build: `docker build -t <image_tag> -f <dockerfile> <context_dir>`

### SSH Backend

- Connection sharing: SSH ControlMaster multiplexing
- Master socket: `/tmp/sandbox-mcp-ssh-<name>`
- Shell process: `ssh -o ControlPath=<socket> <user>@<host> bash`
- ssh_connect: establish ControlMaster connection (key auth only)
- ssh_disconnect: `ssh -S <socket> -O exit <user>@<host>`
- ssh_reconnect: re-establish ControlMaster (shells are lost on disconnect)
- ssh_remove: disconnect + unregister from registry
- No commit/build support (SSH backend only)
- No password authentication in v1 (key-based auth via `key` parameter)

## Default Machine Model

- `sandbox_env(action="default_set", params={machine:"dev"})` sets default machine
- `sandbox_env(action="default_set", params={shell_id:"sh_abc"})` sets that shell as the default shell for its machine
- machine-aware core tools (`sandbox_shell_exec`, `sandbox_file_*`) accept optional `machine` parameter
- If no machine specified: use default machine
- If explicit machine specified: use that machine, don't change default machine
- If no machine and no default machine: error
- `sandbox_shell_exec` without `shell_id` uses the machine's default shell, lazily creating one if needed

## Project Structure (updated)

```
sandbox-mcp/
├── server.py              # MCP server entry + tool dispatch
├── machine_registry.py     # machine management (name -> backend)
├── shell_registry.py      # Shell session management (shell_id -> ShellSession)
├── shell_session.py       # ShellSession: drain thread, dual markers, state machine
├── sandbox_env.py         # sandbox_env action dispatch + help generation
├── file_operations.py     # File ops: read/write/patch/search via shell
├── backends/
│   ├── __init__.py
│   ├── base.py            # Abstract Backend interface
│   ├── docker_backend.py  # Docker implementation
│   └── ssh_backend.py     # SSH implementation
├── pyproject.toml
├── README.md
├── docs/
│   ├── design-spec-v2.md      # this file
│   └── implementation-plan.md # implementation plan
└── tests/
```

## Initial Scope

### Included
- Docker backend: run, stop, start, remove, commit, build
- SSH backend: connect, disconnect, reconnect, remove
- Shell management: shell_exec (wait/no-wait), shell_read, shell_new, shell_remove, shell_list
- Dual marker execution confirmation
- Background drain thread with head+tail buffer
- File operations: read, write, patch, search
- sandbox_env progressive discovery (tools/list -> help/status -> docker_help/ssh_help)
- Default targeting model with default machine/default shell
- Output truncation (tail, configurable max_output)
- Manual shell cleanup with shell_list hints

### Not Included (future versions)
- PTY mode for interactive CLI tools
- Docker image listing
- machine/shell recovery after MCP server restart
- Docker network management
- Docker Compose support
- Resource limits (CPU/memory) per machine
- Security: dangerous command detection, sudo blocking (see Security section below)
- Access control / sandboxing of dangerous commands

## Security (deferred)

Hermes has extensive security guards (dangerous command detection, sudo blocking,
sensitive path protection, approval system). sandbox-mcp v2 does NOT replicate
these in V1 because:

- Sandbox environments are isolated (Docker containers / SSH to dedicated machines)
- The agent is the trusted operator inside the sandbox
- Adding guards adds complexity without clear value in isolated environments

Future versions may add optional guards if sandbox-mcp is used in less trusted
contexts.

## Implementation Language

Python 3.12+. Uses the `mcp` Python SDK, `docker` CLI via subprocess, system
`ssh` with ControlMaster, pytest.
