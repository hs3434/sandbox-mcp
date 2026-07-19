# Flatten Progressive Discovery — Promote Env Actions to Top-Level Tools

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote `shell_new`, `shell_remove`, `shell_list`, `machine_list`, `default_set` from `env()` sub-actions to top-level MCP tools, so agents can discover and use them without multi-step progressive discovery. Add `bash_pid` to `shell_list` output for diagnostics. Enhance busy-shell errors with explicit escape instructions.

**Architecture:** New top-level tool definitions in `TOOL_DEFINITIONS` with handler methods in `SandboxServer` that delegate to existing `SandboxEnv.dispatch()`. No new backend logic — pure re-wiring.

**Tech Stack:** Python, MCP SDK, existing sandbox-mcp codebase

---

### Task 1: Add new top-level tool definitions

**Files:**
- Modify: `src/sandbox_mcp/server.py:244` (after `env` tool definition)

- [ ] **Step 1: Add tool definitions**

After the `env` tool definition (line 267), add these five tool definitions to `TOOL_DEFINITIONS`:

```python
    {
        "name": "shell_new",
        "description": "Create an additional shell session on a machine. Use when the default shell is busy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "machine": {
                    "type": "string",
                    "description": "Machine name (default: default machine)",
                },
                "purpose": {"type": "string", "description": "Human-readable label"},
            },
        },
    },
    {
        "name": "shell_remove",
        "description": "Terminate and remove a shell session (any state: idle, busy, running, terminated).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "shell_id": {"type": "string", "description": "Shell to remove"},
            },
            "required": ["shell_id"],
        },
    },
    {
        "name": "shell_list",
        "description": "List all shell sessions with shell_id, machine, status, bash_pid, last_command, is_default.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "machine": {"type": "string", "description": "Filter by machine (optional)"},
            },
        },
    },
    {
        "name": "machine_list",
        "description": "List all registered machines with backend, status, purpose, shell count, and uptime.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "default_set",
        "description": "Set the default machine or default shell for a machine. Pass exactly one of machine or shell_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "machine": {
                    "type": "string",
                    "description": "Machine name to set as default",
                },
                "shell_id": {
                    "type": "string",
                    "description": "Shell ID to set as default for its machine",
                },
            },
        },
    },
```

- [ ] **Step 2: Run test_lint and verify**

Run: `python -m py_compile src/sandbox_mcp/server.py`
Expected: no syntax errors

---

### Task 2: Add handler methods in SandboxServer

**Files:**
- Modify: `src/sandbox_mcp/server.py:559` (after `_handle_file_search`)

- [ ] **Step 1: Add five handler methods**

After `_handle_file_search` (line 559), add:

```python
    def _handle_shell_new(self, args):
        return self.sandbox_env.dispatch("shell_new", args)

    def _handle_shell_remove(self, args):
        return self.sandbox_env.dispatch("shell_remove", args)

    def _handle_shell_list(self, args):
        return self.sandbox_env.dispatch("shell_list", args)

    def _handle_machine_list(self, args):
        return self.sandbox_env.dispatch("machine_list", args)

    def _handle_default_set(self, args):
        return self.sandbox_env.dispatch("default_set", args)
```

- [ ] **Step 2: Run test_lint again**

Run: `python -m py_compile src/sandbox_mcp/server.py`
Expected: no syntax errors

---

### Task 3: Update list_tools test to include new tools

**Files:**
- Modify: `tests/test_server.py:48-62`

- [ ] **Step 1: Update the expected tool names in test_list_tools_includes_audit_query_by_default**

Change the `expected` set from:
```python
expected = {
    "shell_exec",
    "shell_read",
    "file_read",
    "file_write",
    "file_patch",
    "file_search",
    "env",
    "audit_query",
}
```
to:
```python
expected = {
    "shell_exec",
    "shell_read",
    "file_read",
    "file_write",
    "file_patch",
    "file_search",
    "env",
    "audit_query",
    "shell_new",
    "shell_remove",
    "shell_list",
    "machine_list",
    "default_set",
}
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_server.py::test_list_tools_includes_audit_query_by_default -v`
Expected: PASS

---

### Task 4: Add bash_pid to shell_list output for busy/running shells

**Files:**
- Modify: `src/sandbox_mcp/shell_registry.py:109-127`

- [ ] **Step 1: Add bash_pid field**

In `list_shells()`, after the `item = {...}` dict and before `if session.state == "terminated"`, add:

```python
            if session.state in ("busy", "running"):
                item["bash_pid"] = session.bash_pid
```

- [ ] **Step 2: Update test_list_shells_by_machine to expect bash_pid**

Change the `mock2` in `test_list_shells_by_machine` to also set `bash_pid`:
```python
mock2 = MagicMock(state="running", purpose="tests", uptime=0, last_command="pytest", bash_pid=12345)
```
And add an assertion:
```python
mock2_shell = next(s for s in dev_shells if s["purpose"] == "tests")
assert mock2_shell["bash_pid"] == 12345
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_shell_registry.py -v`
Expected: PASS

---

### Task 5: Enhance busy-shell error with escape_routes in shell_exec handler

**Files:**
- Modify: `src/sandbox_mcp/server.py:498-503`

- [ ] **Step 1: Wrap send() result to inject shell_id and escape routes**

Change `_handle_shell_exec` at line 503 from:
```python
        result = session.send(args["command"], wait=wait, timeout=timeout, max_output=max_output)
```
to:
```python
        result = session.send(args["command"], wait=wait, timeout=timeout, max_output=max_output)
        if result.get("status") == "error" and "busy" in result.get("error", ""):
            result["shell_id"] = shell_id or sid
            result["escape_routes"] = {
                "shell_new": "Create a fresh shell with shell_new()",
                "shell_remove": "Kill and remove this shell with shell_remove(shell_id=...)"
            }
        return result
```

Note: `sid` needs to be accessible — restructure the handler to keep it in scope. Change:

```python
        else:
            machine = self._resolve_machine(args)
            backend = self.machines.get_backend(machine)
            try:
                sid = self.shells.get_or_create_default(
                    machine, lambda: backend.open_shell(machine)
                )
            except ShellUnhealthy as e:
                ...
                return ...
            except Exception as e:
                ...
                return ...
            session = self.shells.get(sid)
```
The variable `sid` is already named `sid` at line 484 — it's in scope. For the `shell_id` branch, the original `shell_id` variable is already available. So:

```python
    def _handle_shell_exec(self, args):
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
            try:
                shell_id = self.shells.get_or_create_default(
                    machine, lambda: backend.open_shell(machine)
                )
            except ShellUnhealthy as e:
                return {
                    "status": "error",
                    "error_kind": "shell_unhealthy",
                    "error": f"[machine={machine!r}] {e}",
                    "machine": machine,
                }
            except Exception as e:
                return {
                    "status": "error",
                    "error_kind": "shell_create_failed",
                    "error": f"[machine={machine!r}] {e}",
                    "machine": machine,
                }
            session = self.shells.get(shell_id)

        result = session.send(args["command"], wait=wait, timeout=timeout, max_output=max_output)
        if result.get("status") == "error" and "busy" in result.get("error", ""):
            result["shell_id"] = shell_id
            result["escape_routes"] = {
                "shell_new": "Create a fresh shell with shell_new()",
                "shell_remove": f"Kill and remove this shell with shell_remove(shell_id='{shell_id}')"
            }
        return result
```

Key change: rename `sid` to `shell_id` in the else branch so the same variable name covers both paths.

- [ ] **Step 2: Update test_sandbox_env_help test**

Run: `pytest tests/test_server.py::test_sandbox_env_help -v`
Expected: PASS (still works, help output unchanged)

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -v`
Expected: all tests PASS

---

### Task 6: Commit

- [ ] **Step 1: Commit all changes**

```bash
git add src/sandbox_mcp/server.py src/sandbox_mcp/shell_registry.py tests/test_server.py tests/test_shell_registry.py
git commit -m "refactor: promote env management actions to top-level tools

- Add shell_new, shell_remove, shell_list, machine_list, default_set as top-level MCP tools
- Each delegates to existing SandboxEnv.dispatch() — no new backend logic
- Add bash_pid to shell_list output for busy/running shells
- Enhance busy-shell error with shell_id and escape_routes for emergency recovery"
```
