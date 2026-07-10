# Sandbox MCP v2 Design Spec

> Supersedes `design-spec.md`. Reflects the redesigned tool architecture from the
> 2026-07-10 design review.

## Problem (unchanged)

Hermes Agent's built-in terminal tool creates ephemeral containers that reset on
recreation. The agent cannot define persistent environments, deploy long-running
services, or manage processes reliably. The built-in file/terminal/code-execution
tools also consume context window space.

## What Changed from v1

| Aspect | v1 (19 tools) | v2 (8 tools) |
|--------|---------------|---------------|
| tools/list count | 19 | 8 (7 core + 1 envtools) |
| Context per API call | ~2850 tokens | ~1000 tokens |
| Management ops | All exposed directly | Lazy discovery via envtools |
| Shell exec + write | Separate tools (exec + shell_write) | Merged into shell_send (wait param) |
| Lifecycle ops | Generic stop/start/remove | Backend-specialized (docker_stop/ssh_disconnect) |
| Shell I/O confirmation | Single marker (end only) | Dual marker (start + end) |
| Output buffer | Unbounded pipe reads | Drain thread, head 5K + tail ring buffer |
| Shell cleanup | Automatic | Manual (agent-controlled, shell_list hints) |

## Architecture

```
Hermes Gateway (host process)
  └── MCP Client (JSON-RPC over stdio)
        └── Sandbox MCP Server (host process)
              ├── tools/list (8 tools, ~1000 tokens/轮)
              │     ├── Core: shell_send, shell_read, shell_open
              │     ├── Core: read, write, patch, search
              │     └── Entry: envtools
              │
              └── envtools (lazy discovery, 15 actions)
                    ├── help / status
                    ├── use / shell_close / shell_list
                    ├── docker_run / docker_build / docker_commit
                    │   docker_stop / docker_start / docker_remove
                    └── ssh_connect / ssh_disconnect / ssh_reconnect / ssh_remove
```

## Three-Layer Tool Exposure

### Layer 1: tools/list (always exposed, ~1000 tokens)

8 tools with simple, well-defined schemas:

| Tool | Purpose | Frequency |
|------|---------|-----------|
| `sandbox_shell_send` | Send command to shell (wait or non-blocking) | High |
| `sandbox_shell_read` | Non-blocking read of shell output | High |
| `sandbox_shell_open` | Open new persistent shell | Medium |
| `sandbox_read` | Read file with line numbers + pagination | High |
| `sandbox_write` | Write file (full content) | High |
| `sandbox_patch` | Targeted find-and-replace (fuzzy match) | High |
| `sandbox_search` | Ripgrep content search + glob file search | High |
| `envtools` | Environment management entry point | Low |

All core tools accept an optional `target` parameter (default: active target set
via `envtools(action="use")`).

### Layer 2: envtools help (on-demand, ~200 tokens)

`envtools(action="help")` returns:
- `use`: set active target
- `status`: check current state (targets, active target, shells)
- `shell_close`: close shell session
- `shell_list`: list shells
- Pointers to `docker_help` and `ssh_help`

### Layer 3: backend-specific help (on-demand, ~400 tokens each)

- `envtools(action="docker_help")`: docker_run, docker_build, docker_commit,
  docker_stop, docker_start, docker_remove
- `envtools(action="ssh_help")`: ssh_connect, ssh_disconnect, ssh_reconnect,
  ssh_remove

Agent only loads the backend help it needs. A Docker-only agent never loads
SSH docs.

### envtools inputSchema (~100 tokens in tools/list)

```json
{
  "name": "envtools",
  "description": "环境管理工具。调action=help查看通用操作,action=docker_help/ssh_help查看后端操作,action=status查看状态。核心工具已直接暴露,支持可选target参数。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "action": {"type": "string", "description": "操作名"},
      "params": {"type": "object", "description": "操作参数,调action=help获取格式"}
    },
    "required": ["action"]
  }
}
```

### Context cost comparison

```
v1: 19 tools × ~150 tokens = ~2850 tokens/轮 (every API call)
v2: 8 tools × ~125 tokens = ~1000 tokens/轮 (every API call)
    + help (200 tokens, once) + docker_help (400 tokens, once) = ~1600 total

Savings: ~44% on every API call, more for agents that don't need all backends.
```

## Shell Design

### sandbox_shell_send

Replaces v1's `sandbox_exec` + `shell_write` with a single tool.

```
shell_send(command, shell_id?, target?, wait=true, timeout=30, max_output=50000)
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

shell_open() -> idle

shell_send(wait=true):
  idle -> busy (acquire lock, send command, wait for __END_)
  busy -> idle (__END_ found)
  busy -> running (timeout, lock released, command still running)

shell_send(wait=false):
  idle -> running (send command, wait for __START_, release)

shell_read():
  running -> running (no __END_ yet)
  running -> idle (__END_ found by drain thread)
  idle -> idle (no output)
  any -> terminated (EOF detected by drain thread)

shell_close():
  any -> removed from registry (no state, shell gone)

bash process exits/dies:
  any -> terminated (passive close, shell stays in registry)
```

**Concurrency rules:**

| Shell state | shell_send | shell_read | shell_close |
|-------------|-----------|------------|-------------|
| idle | Allowed | Returns empty | Allowed |
| busy | Rejected ("Shell is busy") | Rejected | Allowed (interrupts) |
| running | Rejected ("Shell is busy") | Allowed | Allowed |
| terminated | Rejected ("Shell terminated") | Returns remaining + terminated | Allowed (cleanup) |

Per-shell lock ensures atomic state transitions. shell_send(wait=true) holds the
lock during blocking read; other operations on the same shell are rejected.

### Background Drain Thread

Each shell has a daemon drain thread that continuously reads from the bash
process's stdout pipe.

**Why needed:** Linux pipe buffer is 64KB. Without continuous draining, a
command producing >64KB output with no shell_read calls would block on write(),
causing deadlock.

**Buffer strategy: head 5KB + tail ring buffer**

```
drain thread ring buffer:
├── head: first 5KB (fixed, never discarded - gives command context)
├── tail: last ~45KB (ring buffer, old data discarded as new arrives)
└── total memory per shell: ~50KB

When output exceeds 50KB:
  head_5KB + "\n[...truncated...]\n" + tail_45KB
```

**Marker detection:** drain thread scans for __START_ and __END_ markers:
- __START_ found: signal shell_send(wait=false) to return confirmed=true
- __END_ found: extract exit_code, set state idle (if running), signal
  shell_send(wait=true) to return
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
shell_send(command, ..., max_output=50000)  # default 50KB
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
explicitly calls `shell_close`. This prevents losing diagnostic information.

`shell_list` includes hints for terminated shells:

```json
[
  {"shell_id": "sh_abc", "target": "dev", "status": "idle", "uptime": "5m"},
  {"shell_id": "sh_def", "target": "dev", "status": "terminated",
   "hint": "Process exited. Call shell_close to clean up."}
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

## envtools Discovery

### action="help" (static, ~200 tokens)

```json
{
  "operations": [
    {
      "action": "use",
      "description": "设置活动目标。核心工具不传target时使用此目标",
      "required": {"target": "string"},
      "example": {"target": "dev"}
    },
    {
      "action": "status",
      "description": "查看当前状态:活动目标、目标列表、shell列表",
      "params": {}
    },
    {
      "action": "shell_close",
      "description": "关闭shell会话,终止bash进程。用于清理terminated状态的shell",
      "required": {"shell_id": "string"}
    },
    {
      "action": "shell_list",
      "description": "列出所有shell,可选按target过滤",
      "optional": {"target": "string"}
    }
  ],
  "more_help": {
    "docker_help": "Docker: 创建/构建/提交/停止/启动/删除容器",
    "ssh_help": "SSH: 连接/断开/重连/删除远程目标"
  },
  "note": "核心工具(shell_send/shell_read/shell_open/read/write/patch/search)已直接暴露,支持可选target参数。"
}
```

### action="status" (dynamic)

```json
{
  "active_target": "dev",
  "targets": [
    {"name": "dev", "backend": "docker", "status": "running",
     "purpose": "Python dev", "shells": 2, "uptime": "2h15m"}
  ],
  "shells": [
    {"shell_id": "sh_abc", "target": "dev", "status": "idle", "purpose": "default", "uptime": "5m"},
    {"shell_id": "sh_def", "target": "dev", "status": "terminated",
     "hint": "Process exited. Call shell_close to clean up."}
  ]
}
```

### action="docker_help" (static, ~400 tokens)

Operations: docker_run, docker_build, docker_commit, docker_stop,
docker_start, docker_remove. Each with required/optional params, returns, example.

### action="ssh_help" (static, ~200 tokens)

Operations: ssh_connect, ssh_disconnect, ssh_reconnect, ssh_remove.
Each with required/optional params, returns, example.

### Backend-specialized lifecycle operations

v1 had generic `stop`/`start`/`remove` that dispatched by backend. v2
specializes them:

| v1 (generic) | v2 Docker | v2 SSH | Semantic difference |
|---|---|---|---|
| stop | docker_stop | ssh_disconnect | Container stops vs connection closes |
| start | docker_start | ssh_reconnect | Container restarts vs connection re-establishes |
| remove | docker_remove | ssh_remove | Container destroyed vs target unregistered |

Action name itself indicates the backend and behavior. No dispatch ambiguity.
Error messages can be specific: "docker_stop only works on Docker targets".

### Agent discovery flow

```
1. envtools(action="help")            → use + status + shell_close/list + pointers (~200 tokens)
2. envtools(action="status")          → current targets and shells
3. envtools(action="docker_help")     → only if Docker needed (~400 tokens)
4. envtools(action="docker_run", ...)  → create container
5. envtools(action="use", ...)         → set active target
6. sandbox_shell_send(command="...")   → work with core tools
   ...
7. envtools(action="docker_stop", ...) → stop when done
```

## Complete envtools Action List

```
Discovery:  help / status
General:    use / shell_close / shell_list
Docker:     docker_run / docker_build / docker_commit
            docker_stop / docker_start / docker_remove
SSH:        ssh_connect / ssh_disconnect / ssh_reconnect / ssh_remove
```

15 actions, 1 tool in tools/list. Agent loads docs on demand.

## Backend Implementation

### Docker Backend

- Container naming: `sandbox-<target_name>` (deterministic, allows reconnection)
- Shell process: `docker exec -i <container> bash`
- Container lifecycle: `docker run -d --name sandbox-<name> --init --restart
  on-failure:3 <image> sleep infinity`
- docker_stop: `docker stop sandbox-<name>`
- docker_start: `docker start sandbox-<name>`
- docker_remove: `docker rm -f sandbox-<name>`
- docker_commit: `docker commit sandbox-<name> <image_tag>`
- docker_build: `docker build -t <image_tag> -f <dockerfile> <context_dir>`

### SSH Backend

- Connection sharing: SSH ControlMaster multiplexing
- Master socket: `/tmp/sandbox-mcp-ssh-<name>`
- Shell process: `ssh -o ControlPath=<socket> <user>@<host> bash`
- ssh_connect: establish ControlMaster connection
- ssh_disconnect: `ssh -S <socket> -O exit <user>@<host>`
- ssh_reconnect: re-establish ControlMaster (shells are lost on disconnect)
- ssh_remove: disconnect + unregister from registry
- No commit/build support (SSH backend only)

## Hybrid Targeting Model (unchanged from v1)

- `envtools(action="use", params={target:"dev"})` sets active target
- Core tools (shell_send, read, write, etc.) accept optional `target` parameter
- If no target specified: use active target
- If explicit target specified: use that target, don't change active target
- If no target and no active target: error

## Project Structure (updated)

```
sandbox-mcp/
├── server.py              # MCP server entry + tool dispatch
├── target_registry.py     # Target management (name -> backend)
├── shell_registry.py      # Shell session management (shell_id -> ShellSession)
├── shell_session.py       # ShellSession: drain thread, dual markers, state machine
├── envtools.py            # envtools action dispatch + help generation
├── file_operations.py     # File ops: read/write/patch/search via shell
├── backends/
│   ├── __init__.py
│   ├── base.py            # Abstract Backend interface
│   ├── docker_backend.py  # Docker implementation
│   └── ssh_backend.py     # SSH implementation
├── pyproject.toml
├── README.md
├── docs/
│   ├── design-spec.md         # v1 design (superseded)
│   ├── design-spec-v2.md      # this file
│   └── implementation-plan.md # v1 plan (to be updated)
└── tests/
```

## V1 Scope (updated for v2)

### Included
- Docker backend: run, stop, start, remove, commit, build
- SSH backend: connect, disconnect, reconnect, remove
- Shell management: shell_send (wait/no-wait), shell_read, shell_open, shell_close, shell_list
- Dual marker execution confirmation
- Background drain thread with head+tail buffer
- File operations: read, write, patch, search
- envtools lazy discovery (3-level: help -> docker_help/ssh_help)
- Hybrid targeting model
- Output truncation (tail, configurable max_output)
- Manual shell cleanup with shell_list hints

### Not Included (future versions)
- PTY mode for interactive CLI tools
- Docker image listing
- Target/shell recovery after MCP server restart
- Docker network management
- Docker Compose support
- Resource limits (CPU/memory) per target
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
