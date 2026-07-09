# Sandbox Environment Manager MCP - Design Spec

## Problem

Hermes Agent's built-in terminal tool creates ephemeral Docker containers that reset
on every recreation. The agent cannot define persistent environments, deploy
long-running services, or manage processes reliably inside sandboxes. The built-in
file/terminal/code-execution tools also consume context window space that could be
freed by replacing them with a single unified MCP tool.

## Solution

An MCP (Model Context Protocol) server that acts as a **Sandbox Environment Manager**.
It manages persistent execution targets (Docker containers + SSH machines), provides
shell-based command execution with real-time I/O, and replicates the full file
operation capabilities of Hermes' built-in tools.

The MCP server runs on the host as a stdio process, with direct access to the Docker
socket and SSH client. It is registered in Hermes' `config.yaml` under `mcp_servers`.

## Architecture

```
Hermes Gateway (host process)
  └── MCP Client (JSON-RPC over stdio)
        └── Sandbox MCP Server (host process)
              ├── Target Registry (in-memory: name -> target)
              ├── Shell Registry (in-memory: shell_id -> ShellSession)
              ├── DockerBackend
              │     ├── docker run / stop / start / rm / commit / build
              │     └── docker exec -i <container> bash  (persistent shell)
              ├── SSHBackend
              │     ├── ssh connection (ControlMaster multiplexing)
              │     └── ssh user@host bash  (persistent shell)
              └── FileOperations (via shell exec: sed/cat/rg/patch)
```

### Key Design Decisions

1. **Multi-backend**: Docker and SSH are both first-class backends. Both expose the
   same interface: "start a bash process, hold its stdin/stdout pipes." The agent
   does not care which backend a target uses after creation.

2. **Separate creation tools per backend**: `sandbox_docker_run`,
   `sandbox_docker_build`, `sandbox_ssh_connect` -- each has clean, backend-specific
   parameters. Common operations (`exec`, `stop`, `start`, `remove`) are unified and
   dispatch by target name.

3. **Hybrid targeting model**: `sandbox_use(target)` sets an active target. Subsequent
   calls without an explicit `target` parameter go to the active one. Any call can
   override with an explicit `target` parameter without changing the active target.

4. **Shell-based execution, not process-based**: No foreground/background distinction
   at the API level. The tool provides "open shell" and "exec in shell." The agent
   decides whether to use shell job control (`&`, `jobs`, `kill %1`) or dedicated
   shells for concurrent work. This replaces Hermes' `process` tool entirely.

5. **Full file operation replication**: `sandbox_read`/`sandbox_write`/`sandbox_patch`/
   `sandbox_search` replicate all capabilities of Hermes' built-in file tools (line
   numbers, binary detection, fuzzy matching, syntax checking, ripgrep search).

6. **Stateful MCP server**: The server maintains a Target Registry and Shell Registry
   in memory. This is necessary for persistent shell sessions and target management.

## Complete API (19 tools)

### Backend-Specific: Creation & Backend-Unique Operations

#### `sandbox_docker_run`
Create and start a Docker container as a managed target.

Parameters:
- `name` (string, required) -- unique target name
- `image` (string, required) -- Docker image (e.g. "python:3.12")
- `purpose` (string, required) -- human-readable description of what this target is for
- `volumes` (array of strings, optional) -- bind mounts, e.g. `["/host/path:/container/path"]`
- `ports` (array of strings, optional) -- port mappings, e.g. `["8080:8080"]`
- `env` (object, optional) -- environment variables
- `workdir` (string, optional) -- working directory (default: "/workspace")

Returns: `{name, status: "running", backend: "docker"}`

#### `sandbox_docker_build`
Build a custom Docker image from a Dockerfile.

Parameters:
- `image_tag` (string, required) -- tag for the built image (e.g. "my-env:latest")
- `dockerfile` (string, required) -- Dockerfile content (multi-line string)
- `context_dir` (string, optional) -- build context directory on host (default: none)

Returns: `{image_tag, status: "built"}`

#### `sandbox_docker_commit`
Save a running container's current state as a new image. This is how the agent
persists installed packages and environment changes across container recreations.

Parameters:
- `target` (string, required) -- target name
- `image_tag` (string, optional) -- tag for the new image (default: `sandbox-<target>-snapshot:<timestamp>`)

Returns: `{image_tag, status: "committed"}`

#### `sandbox_ssh_connect`
Register an SSH remote machine as a managed target.

Parameters:
- `name` (string, required) -- unique target name
- `host` (string, required) -- hostname or IP
- `user` (string, required) -- SSH user
- `port` (integer, optional) -- SSH port (default: 22)
- `key` (string, optional) -- path to SSH private key
- `password` (string, optional) -- SSH password (if not using key)
- `purpose` (string, required) -- human-readable description

Returns: `{name, status: "connected", backend: "ssh"}`

### Common: Target Management

#### `sandbox_list`
List all managed targets.

Parameters: none

Returns: array of:
```
{
  "name": "dev",
  "backend": "docker",      // or "ssh"
  "status": "running",      // or "stopped", "error"
  "purpose": "Python dev environment",
  "shells": 2,              // number of open shell sessions
  "uptime": "2h15m"         // for running targets
}
```

#### `sandbox_use`
Set the active target. Subsequent calls without `target` parameter use this target.

Parameters:
- `target` (string, required) -- target name

Returns: `{active_target: "dev"}`

#### `sandbox_stop`
Stop a target (docker stop / ssh disconnect). State is preserved.

Parameters:
- `target` (string, optional) -- target name (default: active target)

Returns: `{target, status: "stopped"}`

#### `sandbox_start`
Start a stopped target (docker start / ssh reconnect).

Parameters:
- `target` (string, optional) -- target name (default: active target)

Returns: `{target, status: "running"}`

#### `sandbox_remove`
Remove a target. Docker: stops and removes the container. SSH: unregisters from
registry (does not touch the remote machine). All open shells for the target are
closed.

Parameters:
- `target` (string, optional) -- target name (default: active target)

Returns: `{target, status: "removed"}`

### Common: Command Execution & Shell Management

#### `sandbox_exec`
Execute a command in a shell. Uses the target's default shell if no `shell_id`
is specified. The command runs in the specified shell's bash process, inheriting
its state (cwd, env vars, aliases).

Parameters:
- `command` (string, required) -- shell command to execute
- `target` (string, optional) -- target name (default: active target)
- `shell_id` (string, optional) -- specific shell to run in (default: target's default shell)
- `timeout` (integer, optional) -- seconds to wait for command completion (default: 30)

Returns:
```
// Command completed within timeout:
{"output": "...", "exit_code": 0, "status": "completed"}

// Command still running after timeout (shell is still alive, agent can read more output):
{"output": "partial...", "exit_code": null, "status": "running"}
```

Implementation: writes `command\necho __CMD_<uuid>__:$?\n` to the shell's stdin,
reads stdout until the `__CMD_<uuid>__:<exit_code>` marker or timeout. On timeout,
returns partial output; the command continues running in the shell.

#### `sandbox_shell_open`
Open a new persistent shell session on a target. Each shell is an independent bash
process (like a new terminal tab). Use this for concurrent work or long-running
processes.

Parameters:
- `target` (string, optional) -- target name (default: active target)
- `purpose` (string, optional) -- description of what this shell is for

Returns: `{"shell_id": "sh_<uuid>", "target": "dev"}`

Implementation:
- Docker: `docker exec -i <container> bash`
- SSH: `ssh <user>@<host> bash` (via ControlMaster multiplexed connection)

The server holds the stdin/stdout/stderr pipes of this process.

#### `sandbox_shell_close`
Close a shell session. Kills the underlying bash process and releases pipes.

Parameters:
- `shell_id` (string, required)

Returns: `{"shell_id": "sh_xxx", "status": "closed"}`

#### `sandbox_shell_list`
List all open shell sessions, optionally filtered by target.

Parameters:
- `target` (string, optional) -- filter by target name

Returns: array of:
```
{
  "shell_id": "sh_abc123",
  "target": "dev",
  "purpose": "run tests",
  "status": "running",    // or "idle" (command finished, shell waiting)
  "uptime": "5m",
  "last_command": "pytest -v"
}
```

#### `sandbox_shell_read`
Read new output from a shell's stdout. Non-blocking: returns whatever has been
buffered since the last read. Use this to check on long-running commands started
with `sandbox_exec` that returned `status: "running"`.

Parameters:
- `shell_id` (string, required)

Returns:
```
{
  "output": "new output since last read...",
  "eof": false   // true if the shell process has exited
}
```

#### `sandbox_shell_write`
Write data to a shell's stdin. Use this to interact with processes that read stdin
(answer prompts, send commands to a REPL, etc.).

Parameters:
- `shell_id` (string, required)
- `data` (string, required) -- raw data to write to stdin

Returns: `{"shell_id": "sh_xxx", "bytes_written": 5}`

### File Operations (replicates Hermes built-in file tools)

All file operations accept an optional `target` parameter (default: active target).
They execute shell commands in the target's default shell (or a one-off exec if the
default shell is busy), replicating the behavior of Hermes' `ShellFileOperations`.
File operations do not accept `shell_id` -- they always use the target's default
execution path.

#### `sandbox_read`
Read a text file with line numbers and pagination.

Parameters:
- `path` (string, required) -- file path (absolute, relative, or ~/path)
- `target` (string, optional) -- target name (default: active target)
- `offset` (integer, optional) -- line number to start from (1-indexed, default: 1)
- `limit` (integer, optional) -- max lines to read (default: 500, max: 2000)

Returns: `LINE_NUM|CONTENT` formatted text, with truncation hints for large files.
Binary files are detected and rejected. Image files are flagged with a hint to use
vision tools. Similar file names are suggested when the file is not found.

Implementation: `wc -c` (exists check) -> `head -c 1000` (binary detection) ->
`sed -n 'offset,end p'` (read with pagination) -> `wc -l` (total lines).

#### `sandbox_write`
Write content to a file, completely replacing existing content.

Parameters:
- `path` (string, required) -- file path
- `content` (string, required) -- complete file content
- `target` (string, optional) -- target name (default: active target)

Creates parent directories automatically. Writes via stdin pipe to `cat > path`
(bypasses ARG_MAX for large files). Runs syntax checks on `.py`/`.json`/`.yaml`/
`.toml` files after writing; only newly introduced errors are surfaced.

#### `sandbox_patch`
Targeted find-and-replace edits in files.

Parameters:
- `mode` (string, required) -- `"replace"` or `"patch"`
- `path` (string, required for replace mode) -- file path
- `old_string` (string, required for replace mode) -- text to find
- `new_string` (string, required for replace mode) -- replacement text
- `replace_all` (boolean, optional) -- replace all occurrences (default: false)
- `patch` (string, required for patch mode) -- V4A multi-file patch content
- `target` (string, optional) -- target name (default: active target)

Replace mode: fuzzy matching with 9 strategies (handles whitespace/indentation
differences). Returns unified diff of changes. Runs syntax checks after editing.

Patch mode: applies V4A format patches for bulk multi-file changes.

#### `sandbox_search`
Search file contents or find files by name.

Parameters:
- `pattern` (string, required) -- regex pattern (content search) or glob (file search)
- `search_type` (string, optional) -- `"content"` or `"files"` (default: `"content"`)
- `target` (string, optional) -- target name (default: active target)
- `path` (string, optional) -- directory to search in (default: cwd)
- `file_glob` (string, optional) -- filter files by pattern (e.g. `"*.py"`)
- `limit` (integer, optional) -- max results (default: 50)
- `offset` (integer, optional) -- skip first N results (default: 0)
- `output_mode` (string, optional) -- `"content"`, `"files_only"`, `"count"` (default: `"content"`)
- `context` (integer, optional) -- context lines around matches (default: 0)

Ripgrep-backed for content search. File search uses `find` with glob patterns.

## Shell Session Model

### Default Shell

Each target has an implicit "default shell" that is lazily created on the first
`sandbox_exec` call. This shell persists until the target is stopped or removed.
`sandbox_exec` without `shell_id` uses the default shell.

### Additional Shells

`sandbox_shell_open` creates additional bash processes on the target. Each is
independent with its own state (cwd, env vars). Use cases:
- Run a long task while continuing work in the default shell
- Start a server process and interact with its stdin
- Run commands in a different working directory

### Output Delimiting

Each `sandbox_exec` call appends a unique marker after the command:
```
<command>
echo __CMD_<uuid>__:$?
```
The server reads stdout until it finds `__CMD_<uuid>__:<exit_code>`. This reliably
separates command output from shell prompt noise and identifies the exit code.

If the marker is not found within `timeout` seconds, the command is still running.
The server returns partial output with `status: "running"`. The agent can call
`sandbox_shell_read` to get more output or `sandbox_shell_close` to kill it.

**Busy shell behavior**: if a shell's previous command is still running (returned
`status: "running"`), a new `sandbox_exec` on the same shell returns an error. The
agent must either `sandbox_shell_read` to wait for completion, `sandbox_shell_close`
to kill it, or `sandbox_shell_open` to start a new shell for concurrent work.

### Shell States

- **idle**: no command running, bash is at prompt
- **running**: a command is executing (was started by `sandbox_exec` and either
  timed out or the agent opened a new shell and started a long process)
- **closed**: the bash process has exited

## Backend Implementation Details

### Docker Backend

- **Container naming**: `sandbox-<target_name>` (deterministic, not UUID). This
  allows reconnection after MCP server restart.
- **Shell process**: `docker exec -i <container> bash`
- **Container lifecycle**: `docker run -d --name sandbox-<name> --init --restart
  on-failure:3 <image> sleep infinity`
- **Volumes/ports/env**: passed as Docker CLI flags at creation time
- **Commit**: `docker commit <container> <image_tag>`
- **Build**: `docker build -t <image_tag> -f <dockerfile> <context_dir>`

### SSH Backend

- **Connection sharing**: SSH ControlMaster multiplexing. First connection
  establishes a master socket at `/tmp/sandbox-mcp-ssh-<name>`. Subsequent
  shells and exec calls reuse the master connection (no re-authentication).
- **Shell process**: `ssh -o ControlPath=<socket> <user>@<host> bash`
- **Reconnection**: `sandbox_start` for SSH = establish a new ControlMaster
  connection. Shells are lost on disconnect (unlike Docker where the container
  persists).
- **No commit/build**: SSH backend does not support `sandbox_docker_commit` or
  `sandbox_docker_build`. These tools return an error if called on an SSH target.

## Hermes Integration

### MCP Server Registration

In Hermes' `config.yaml` (at `$HERMES_HOME/config.yaml`):

```yaml
mcp_servers:
  sandbox:
    command: python
    args: ["/path/to/sandbox-mcp/server.py"]
```

### Disabling Built-in Tools

To avoid duplicate tool schemas in the agent's context, disable the built-in
terminal/file/code_execution toolsets:

```yaml
agent:
  disabled_toolsets:
    - terminal
    - file
    - code_execution
```

### Not Exposed

The following built-in tools are NOT replaced by this MCP tool (they remain as
built-in): web search, browser, vision, memory, todo, session_search, clarify,
skills, delegate_task, cronjob, image_generate, text_to_speech.

## V1 Scope

### Included
- Docker backend: run, stop, start, remove, commit, build
- SSH backend: connect, disconnect, reconnect, remove
- Shell management: open, close, list, read, write
- Command execution with hybrid targeting
- File operations: read, write, patch, search (full capability replication)
- Shell session persistence within MCP server lifetime

### Not Included (future versions)
- PTY mode for interactive CLI tools
- `sandbox_docker_images` (list available Docker images)
- Target and shell recovery after MCP server restart (Docker containers persist
  on disk but the in-memory registry is lost; a future version could scan for
  `sandbox-*` containers and re-register them)
- Docker network management between containers
- Docker Compose support
- Resource limits (CPU/memory) per target
- Access control / sandboxing of dangerous commands

## Implementation Language

Python. Consistent with Hermes' ecosystem, uses the `mcp` Python SDK for the MCP
server, `docker` CLI via subprocess for Docker operations, and `paramiko` or system
`ssh` for SSH.

## Project Structure

```
sandbox-mcp/
├── server.py              # MCP server entry point (stdio JSON-RPC)
├── target_registry.py     # Target management (name -> backend -> connection)
├── shell_registry.py      # Shell session management (shell_id -> ShellSession)
├── backends/
│   ├── base.py            # Abstract backend interface
│   ├── docker_backend.py  # Docker implementation
│   └── ssh_backend.py     # SSH implementation
├── file_operations.py     # File operations (read/write/patch/search)
├── shell_session.py       # ShellSession class (pipes, output delimiting)
├── pyproject.toml
├── README.md
└── tests/
```
