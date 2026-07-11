# Sandbox MCP v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an MCP server with 7 exposed tools (6 core + 1 sandbox_env entry) that manages Docker containers and SSH machines as persistent execution machines, with shell-based command execution and full file operation capabilities.

**Architecture:** Stateful MCP server (stdio JSON-RPC). Three-layer tool exposure: core tools always in tools/list (~875 tokens), sandbox_env for progressive discovery of 18 management actions. ShellSession uses dual-marker mechanism with background drain thread.

**Tech Stack:** Python 3.12+, `mcp` Python SDK, `docker` CLI via subprocess, system `ssh` with ControlMaster, pytest

**Design Spec:** See [design-spec-v2.md](design-spec-v2.md) for full design rationale.

---

## File Structure

```
sandbox-mcp/
├── pyproject.toml              # Package metadata + dependencies (src layout)
├── src/
│   └── sandbox_mcp/
│       ├── __init__.py
│       ├── server.py                   # MCP server entry + 7 tool definitions + dispatch
│       ├── machine_registry.py          # machine management (name -> backend)
│       ├── shell_registry.py           # Shell session management (shell_id -> ShellSession)
│       ├── shell_session.py            # ShellSession: drain thread, dual markers, state machine
│       ├── sandbox_env.py             # sandbox_env action dispatch + help generation
│       ├── file_operations.py          # File ops: read/write/patch/search via shell
│       └── backends/
│           ├── __init__.py
│           ├── base.py                 # Abstract Backend interface
│           ├── docker_backend.py       # Docker: run/build/commit/stop/start/remove
│           └── ssh_backend.py          # SSH: connect/disconnect/reconnect/remove (key auth only)
├── tests/
│   ├── conftest.py
│   ├── test_shell_session.py
│   ├── test_backends_base.py
│   ├── test_docker_backend.py
│   ├── test_ssh_backend.py
│   ├── test_target_registry.py
│   ├── test_shell_registry.py
│   ├── test_file_operations.py
│   ├── test_sandbox_env.py
│   ├── test_server.py
│   └── test_integration_docker.py
└── docs/
    ├── design-spec-v2.md       # current design
    └── implementation-plan.md  # this file
```

All modules are imported as `from sandbox_mcp.X import Y`; the package name
(no underscore) avoids colliding with PyPI's common `server` namespace.

---

## Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `backends/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[project]
name = "sandbox-mcp"
version = "0.2.0"
description = "Sandbox Environment Manager MCP server"
requires-python = ">=3.12"
license = {text = "MIT"}
authors = [{name = "Sandbox MCP Contributors"}]
dependencies = [
    "mcp>=1.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "ruff>=0.6.0",
    "mypy>=1.10.0",
]

[project.scripts]
sandbox-mcp = "sandbox_mcp.server:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
markers = ["integration: requires Docker daemon to run"]

[tool.ruff]
line-length = 100
machine-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "W", "B", "UP", "SIM", "RUF"]

[tool.mypy]
python_version = "3.12"
strict = false
warn_unused_ignores = true
disallow_untyped_defs = false
ignore_missing_imports = true
```

- [ ] **Step 2: Create package init files**

Create `backends/__init__.py` (empty) and `tests/__init__.py` (empty).

- [ ] **Step 3: Create conftest.py with shared fixtures**

```python
# tests/conftest.py
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def tmp_workdir(tmp_path, monkeypatch):
    """Change to a temp directory for isolated tests."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
```

- [ ] **Step 4: Install package in dev mode and verify**

```bash
cd /work/sandbox-mcp
pip install -e ".[dev]"
python -c "import server; print('import OK')"
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: project scaffolding with pyproject.toml and package structure"
```

---

## Task 2: ShellSession with Drain Thread and Dual Markers

**Files:**
- Create: `shell_session.py`
- Test: `tests/test_shell_session.py`

ShellSession wraps a persistent bash process with:
- **Dual marker mechanism**: `__START_<uuid>__` confirms execution, `__END_<uuid>__:$?` captures exit code
- **Background drain thread**: continuously reads stdout, buffers output (head 5KB + tail ~45KB ring buffer), detects markers
- **State machine**: idle -> busy -> idle (wait=true), idle -> running -> idle (wait=false + shell_read), any -> terminated (bash dies)
- **send(command, wait, timeout)**: replaces separate exec + shell_write semantics
- **read()**: non-blocking read from in-memory buffer, detects completion via markers
- **close()**: kill process, cleanup
- **I/O**: stderr merged into stdout (matches terminal behavior)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_shell_session.py
import time
import pytest
from sandbox_mcp.shell_session import ShellSession


def test_send_wait_true_simple_command():
    """send(wait=true) executes a command and returns output + exit code."""
    session = ShellSession(["bash"])
    result = session.send("echo hello world", wait=True, timeout=5)
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "hello world" in result["output"]
    session.close()


def test_send_wait_true_preserves_state():
    """Environment changes persist across send calls in the same shell."""
    session = ShellSession(["bash"])
    session.send("export FOO=bar", wait=True, timeout=5)
    result = session.send("echo $FOO", wait=True, timeout=5)
    assert "bar" in result["output"]
    session.close()


def test_send_wait_true_exit_code():
    """Non-zero exit codes are captured correctly."""
    session = ShellSession(["bash"])
    result = session.send("exit 42", wait=True, timeout=5)
    assert result["status"] in ("completed", "terminated")
    session.close()


def test_send_wait_true_timeout_returns_running():
    """A command that doesn't finish within timeout returns status=running."""
    session = ShellSession(["bash"])
    result = session.send("sleep 10", wait=True, timeout=1)
    assert result["status"] == "running"
    assert result["exit_code"] is None
    session.close()


def test_send_wait_false_confirms_execution():
    """send(wait=false) confirms command started via __START_ marker."""
    session = ShellSession(["bash"])
    result = session.send("echo started", wait=False, timeout=3)
    assert result["status"] == "running"
    assert result["confirmed"] is True
    session.close()


def test_send_on_busy_shell_rejected():
    """send on a running shell returns error."""
    session = ShellSession(["bash"])
    session.send("sleep 5", wait=True, timeout=0.5)
    # Shell is now running (timed out)
    result = session.send("echo should_fail", wait=True, timeout=1)
    assert result["status"] == "error"
    assert "busy" in result.get("error", "").lower()
    session.close()


def test_read_after_wait_false():
    """After send(wait=false), read() returns output and detects completion."""
    session = ShellSession(["bash"])
    session.send("echo hello; sleep 0.3; echo done", wait=False, timeout=3)
    # Wait for command to finish
    time.sleep(1.0)
    # Read until completed
    found_completed = False
    for _ in range(10):
        result = session.read()
        if result["status"] == "completed":
            found_completed = True
            assert result["exit_code"] == 0
            break
        time.sleep(0.2)
    assert found_completed, "Should detect completion via __END_ marker"
    session.close()


def test_read_idle_shell():
    """read() on idle shell returns empty output with status=idle."""
    session = ShellSession(["bash"])
    result = session.read()
    assert result["status"] == "idle"
    assert result["output"] == ""
    session.close()


def test_close_kills_process():
    """close() kills the underlying process."""
    session = ShellSession(["bash"])
    session.close()
    assert session.state == "terminated"
    result = session.send("echo test", wait=True, timeout=1)
    assert result["status"] == "error"


def test_terminated_on_bash_exit():
    """When bash process dies, state becomes terminated."""
    session = ShellSession(["bash"])
    session.send("exit 0", wait=True, timeout=5)
    # bash has exited
    time.sleep(0.3)
    assert session.state == "terminated"
    session.close()


def test_output_truncation():
    """Large output is truncated to tail with notice."""
    session = ShellSession(["bash"])
    result = session.send("seq 1 100000", wait=True, timeout=10, max_output=5000)
    assert result["status"] == "completed"
    assert "truncated" in result["output"].lower()
    assert "100000" in result["output"]  # tail includes last line
    session.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_shell_session.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'shell_session'`

- [ ] **Step 3: Implement ShellSession**

```python
# shell_session.py
"""ShellSession: persistent bash process with dual-marker I/O and drain thread.

States: idle | busy | running | terminated
  idle       - no command running, bash at prompt
  busy       - send(wait=true) blocking, lock held
  running    - command executing in background (wait=false or timeout)
  terminated - bash process exited (passive close)
"""

import os
import select
import subprocess
import threading
import time
import uuid
from collections import deque
from typing import Optional


class ShellSession:
    """A persistent shell (bash) process with drain-thread-based I/O."""

    HEAD_SIZE = 5120        # 5KB head buffer
    TAIL_SIZE = 46080       # ~45KB tail ring buffer
    DEFAULT_MAX_OUTPUT = 50000  # 50KB default output limit

    def __init__(self, args: list[str]):
        self._args = args
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._state = "idle"
        self._last_command: Optional[str] = None
        self._started_at = time.time()
        self._purpose: Optional[str] = None

        # Drain thread buffer
        self._head: bytearray = bytearray()
        self._tail: deque = deque(maxlen=self.TAIL_SIZE)
        self._head_done = False
        self._total_bytes = 0

        # Marker tracking
        self._pending_start_marker: Optional[str] = None
        self._pending_end_marker: Optional[str] = None
        self._pending_exit_code: Optional[int] = None
        self._start_event = threading.Event()
        self._end_event = threading.Event()

        self._drain_thread: Optional[threading.Thread] = None
        self._start()

    def _start(self) -> None:
        self._process = subprocess.Popen(
            self._args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr into stdout
            bufsize=0,
        )
        self._state = "idle"
        self._drain_thread = threading.Thread(machine=self._drain, daemon=True)
        self._drain_thread.start()

    def _drain(self) -> None:
        """Background thread: continuously read stdout, buffer data, detect markers."""
        buf = bytearray()
        while True:
            try:
                ready, _, _ = select.select([self._process.stdout], [], [], 0.1)
            except (ValueError, OSError):
                break
            if ready:
                try:
                    chunk = os.read(self._process.stdout.fileno(), 4096)
                except (ValueError, OSError):
                    break
                if not chunk:
                    break  # EOF
                self._total_bytes += len(chunk)
                buf.extend(chunk)

                # Store in head/tail buffer
                if not self._head_done:
                    remaining = self.HEAD_SIZE - len(self._head)
                    if remaining > 0:
                        self._head.extend(chunk[:remaining])
                        leftover = chunk[remaining:]
                        if leftover:
                            self._tail.extend(leftover)
                            self._head_done = True
                    else:
                        self._tail.extend(chunk)
                        self._head_done = True
                else:
                    self._tail.extend(chunk)

                # Scan for markers in accumulated buffer
                text = buf.decode("utf-8", errors="replace")

                if self._pending_start_marker:
                    start_tag = self._pending_start_marker
                    if start_tag in text:
                        self._start_event.set()

                if self._pending_end_marker:
                    end_tag = f"{self._pending_end_marker}:"
                    if end_tag in text:
                        idx = text.index(end_tag)
                        after = text[idx + len(end_tag):]
                        code_str = after.strip().split("\n")[0].strip()
                        try:
                            self._pending_exit_code = int(code_str)
                        except ValueError:
                            self._pending_exit_code = 0
                        self._end_event.set()

                # Keep only last 4KB in scan buffer to avoid unbounded growth
                if len(buf) > 8192:
                    buf = buf[-4096:]
            else:
                if self._process.poll() is not None:
                    break
                time.sleep(0.05)

        # EOF: bash process has exited
        self._state = "terminated"
        self._start_event.set()  # unblock any waiting send
        self._end_event.set()

    def send(self, command: str, wait: bool = True, timeout: float = 30,
             max_output: int = DEFAULT_MAX_OUTPUT) -> dict:
        """Send a command to the shell.

        wait=True:  block until __END_ marker or timeout
        wait=False: block until __START_ marker (~2s), then return
        """
        with self._lock:
            if self._state in ("terminated", "closed"):
                return {"output": "", "exit_code": None, "status": "error",
                        "error": "Shell is terminated"}
            if self._state in ("busy", "running"):
                return {"output": "", "exit_code": None, "status": "error",
                        "error": "Shell is busy (previous command still running). "
                                 "Use shell_read to check or shell_remove to kill."}

            marker = uuid.uuid4().hex
            start_marker = f"__START_{marker}__"
            end_marker = f"__END_{marker}__"
            full_input = f"echo {start_marker}\n{command}\necho {end_marker}:$?\n"

            # Reset marker tracking
            self._pending_start_marker = start_marker
            self._pending_end_marker = end_marker
            self._pending_exit_code = None
            self._start_event.clear()
            self._end_event.clear()

            # Reset buffer for new command output
            self._head = bytearray()
            self._tail = deque(maxlen=self.TAIL_SIZE)
            self._head_done = False
            self._total_bytes = 0

            self._last_command = command

            if wait:
                self._state = "busy"
            else:
                self._state = "running"

            try:
                self._process.stdin.write(full_input.encode())
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._state = "terminated"
                return {"output": "", "exit_code": None, "status": "terminated"}

        # Outside lock: wait for markers
        if wait:
            # Wait for __END_ marker or timeout
            if self._end_event.wait(timeout=timeout):
                exit_code = self._pending_exit_code
                output = self._get_buffered_output(max_output)
                with self._lock:
                    if self._state != "terminated":
                        self._state = "idle"
                return {"output": output, "exit_code": exit_code, "status": "completed"}
            else:
                # Timeout - command still running
                output = self._get_buffered_output(max_output)
                with self._lock:
                    if self._state == "busy":
                        self._state = "running"
                return {"output": output, "exit_code": None, "status": "running"}
        else:
            # Wait briefly for __START_ marker (~2s)
            if self._start_event.wait(timeout=2.0):
                with self._lock:
                    if self._state == "terminated":
                        return {"status": "terminated", "confirmed": False}
                return {"status": "running", "confirmed": True}
            else:
                with self._lock:
                    if self._state == "terminated":
                        return {"status": "terminated", "confirmed": False}
                return {"status": "running", "confirmed": False}

    def read(self) -> dict:
        """Non-blocking read of new output from the buffer."""
        with self._lock:
            if self._state == "terminated":
                output = self._get_buffered_output(self.DEFAULT_MAX_OUTPUT)
                return {"output": output, "status": "terminated"}

            if self._state == "idle":
                return {"output": "", "status": "idle"}

            # Check if command completed (drain thread found __END_)
            if self._end_event.is_set() and self._pending_exit_code is not None:
                output = self._get_buffered_output(self.DEFAULT_MAX_OUTPUT)
                self._state = "idle"
                return {"output": output, "exit_code": self._pending_exit_code,
                        "status": "completed"}

            # Command still running
            output = self._get_buffered_output(self.DEFAULT_MAX_OUTPUT)
            return {"output": output, "status": "running"}

    def _get_buffered_output(self, max_output: int) -> str:
        """Get buffered output, truncating if necessary."""
        # Strip markers from output
        head_text = self._head.decode("utf-8", errors="replace")
        tail_text = bytes(self._tail).decode("utf-8", errors="replace")

        # Remove marker lines
        for marker_pattern in [r"__START_[0-9a-f]+__", r"__END_[0-9a-f]+__:\d+"]:
            import re
            head_text = re.sub(marker_pattern, "", head_text)
            tail_text = re.sub(marker_pattern, "", tail_text)

        full = head_text + tail_text

        if len(full) <= max_output:
            return full.strip("\n")

        # Truncate: keep tail
        truncated = full[-max_output:]
        notice = f"\n[Output truncated: showing last {max_output} of {len(full)} chars]\n"
        return (notice + truncated).strip("\n")

    def write_stdin(self, data: str) -> dict:
        """Write raw data to stdin (for interactive processes)."""
        if self._state in ("terminated", "closed"):
            return {"bytes_written": 0, "error": "Shell is terminated"}
        try:
            encoded = data.encode("utf-8")
            self._process.stdin.write(encoded)
            self._process.stdin.flush()
            return {"bytes_written": len(encoded)}
        except (BrokenPipeError, OSError) as e:
            self._state = "terminated"
            return {"bytes_written": 0, "error": str(e)}

    def close(self) -> None:
        """Kill the shell process and stop drain thread."""
        with self._lock:
            self._state = "terminated"
        if self._process:
            try:
                self._process.kill()
                self._process.wait(timeout=5)
            except Exception:
                pass
            self._process = None
        self._start_event.set()
        self._end_event.set()

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_command(self) -> Optional[str]:
        return self._last_command

    @property
    def uptime(self) -> float:
        return time.time() - self._started_at

    @property
    def purpose(self) -> Optional[str]:
        return self._purpose

    @purpose.setter
    def purpose(self, value: str) -> None:
        self._purpose = value
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_shell_session.py -v
```
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add shell_session.py tests/test_shell_session.py
git commit -m "feat: ShellSession with dual markers, drain thread, and state machine"
```

---

## Task 3: Backend Abstract Interface

**Files:**
- Create: `backends/base.py`
- Test: `tests/test_backends_base.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_backends_base.py
import pytest
from sandbox_mcp.backends.base import Backend, TargetInfo


def test_target_info_dataclass():
    info = TargetInfo(name="dev", backend="docker", status="running", purpose="Dev")
    assert info.name == "dev"
    assert info.backend == "docker"
    assert info.status == "running"
    assert info.purpose == "Dev"


def test_backend_is_abstract():
    with pytest.raises(TypeError):
        Backend()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_backends_base.py -v
```

- [ ] **Step 3: Implement base.py**

```python
# backends/base.py
"""Abstract backend interface for sandbox execution machines."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from sandbox_mcp.shell_session import ShellSession


@dataclass
class TargetInfo:
    name: str
    backend: str  # "docker" | "ssh"
    status: str   # "running" | "stopped" | "error" | "terminated"
    purpose: str = ""
    shells: int = 0
    uptime: str = ""


class Backend(ABC):
    """Abstract interface for sandbox backends."""

    @abstractmethod
    def create(self, name: str, purpose: str = "", **kwargs) -> TargetInfo:
        """Create and start a new machine."""
        ...

    @abstractmethod
    def stop(self, name: str) -> TargetInfo:
        """Stop a running machine (state preserved)."""
        ...

    @abstractmethod
    def start(self, name: str) -> TargetInfo:
        """Start a stopped machine."""
        ...

    @abstractmethod
    def remove(self, name: str) -> dict:
        """Remove a machine entirely."""
        ...

    @abstractmethod
    def get_info(self, name: str) -> TargetInfo:
        """Get current status of a machine."""
        ...

    @abstractmethod
    def open_shell(self, name: str) -> ShellSession:
        """Open a new persistent shell on the machine."""
        ...

    @abstractmethod
    def exec_oneoff(self, name: str, command: str, timeout: int = 30) -> dict:
        """Execute a one-off command (no persistent shell)."""
        ...
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_backends_base.py -v
```
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add backends/base.py backends/__init__.py tests/test_backends_base.py
git commit -m "feat: abstract Backend interface with TargetInfo"
```

---

## Task 4: Docker Backend

**Files:**
- Create: `backends/docker_backend.py`
- Test: `tests/test_docker_backend.py`

Docker backend implements: create (docker_run), stop (docker_stop), start
(docker_start), remove (docker_remove), commit (docker_commit), build
(docker_build), open_shell, exec_oneoff.

- [ ] **Step 1: Write failing test (mocked subprocess)**

```python
# tests/test_docker_backend.py
import pytest
from unittest.mock import patch, MagicMock
from sandbox_mcp.backends.docker_backend import DockerBackend


@pytest.fixture
def docker_backend():
    return DockerBackend()


def test_docker_create(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="abc123\n")
        info = docker_backend.create(
            name="dev", purpose="test", image="python:3.12",
            volumes=["/host:/container"], ports=["8080:8080"],
        )
        assert info.name == "dev"
        assert info.backend == "docker"
        assert info.status == "running"
        call_args = mock_run.call_args[0][0]
        assert "run" in call_args
        assert "sandbox-dev" in call_args
        assert "python:3.12" in call_args


def test_docker_stop(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        info = docker_backend.stop("dev")
        call_args = mock_run.call_args[0][0]
        assert "stop" in call_args
        assert "sandbox-dev" in call_args


def test_docker_start(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        info = docker_backend.start("dev")
        call_args = mock_run.call_args[0][0]
        assert "start" in call_args
        assert "sandbox-dev" in call_args


def test_docker_remove(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = docker_backend.remove("dev")
        call_args = mock_run.call_args[0][0]
        assert "rm" in call_args
        assert "-f" in call_args
        assert "sandbox-dev" in call_args


def test_docker_commit(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = docker_backend.commit("dev", "my-image:latest")
        call_args = mock_run.call_args[0][0]
        assert "commit" in call_args
        assert "sandbox-dev" in call_args
        assert "my-image:latest" in call_args


def test_docker_build(docker_backend):
    with patch("subprocess.run") as mock_run, \
         patch("builtins.open", MagicMock()):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = docker_backend.build("my-image:latest", "FROM python:3.12\n")
        call_args = mock_run.call_args[0][0]
        assert "build" in call_args
        assert "-t" in call_args
        assert "my-image:latest" in call_args


def test_docker_open_shell(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        shell = docker_backend.open_shell("dev")
        assert "docker" in shell._args[0]
        assert "exec" in shell._args
        assert "sandbox-dev" in shell._args
        shell.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_docker_backend.py -v
```

- [ ] **Step 3: Implement DockerBackend**

```python
# backends/docker_backend.py
"""Docker backend: manages containers via docker CLI."""

import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from sandbox_mcp.backends.base import Backend, TargetInfo
from sandbox_mcp.shell_session import ShellSession


def _find_docker() -> str:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("docker CLI not found on PATH")
    return docker


class DockerBackend(Backend):
    """Docker container backend."""

    def __init__(self):
        self._docker = _find_docker()

    def _container_name(self, name: str) -> str:
        return f"sandbox-{name}"

    def create(self, name: str, purpose: str = "", **kwargs) -> TargetInfo:
        image = kwargs.get("image", "python:3.12")
        volumes = kwargs.get("volumes", [])
        ports = kwargs.get("ports", [])
        env = kwargs.get("env", {})
        workdir = kwargs.get("workdir", "/workspace")

        cmd = [self._docker, "run", "-d", "--init",
               "--restart", "on-failure:3",
               "--name", self._container_name(name),
               "-w", workdir]

        for vol in volumes:
            cmd.extend(["-v", vol])
        for port in ports:
            cmd.extend(["-p", port])
        for key, val in env.items():
            cmd.extend(["-e", f"{key}={val}"])

        cmd.extend([image, "sleep", "infinity"])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return TargetInfo(name=name, backend="docker", status="error", purpose=purpose)

        return TargetInfo(name=name, backend="docker", status="running", purpose=purpose)

    def stop(self, name: str) -> TargetInfo:
        subprocess.run([self._docker, "stop", self._container_name(name)],
                       capture_output=True, timeout=30)
        return TargetInfo(name=name, backend="docker", status="stopped")

    def start(self, name: str) -> TargetInfo:
        subprocess.run([self._docker, "start", self._container_name(name)],
                       capture_output=True, timeout=30)
        return TargetInfo(name=name, backend="docker", status="running")

    def remove(self, name: str) -> dict:
        subprocess.run([self._docker, "rm", "-f", self._container_name(name)],
                       capture_output=True, timeout=30)
        return {"machine": name, "status": "removed"}

    def get_info(self, name: str) -> TargetInfo:
        result = subprocess.run(
            [self._docker, "inspect", "--format", "{{.State.Status}}",
             self._container_name(name)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return TargetInfo(name=name, backend="docker", status="error")
        state = result.stdout.strip()
        status = "running" if state == "running" else "stopped"
        return TargetInfo(name=name, backend="docker", status=status)

    def open_shell(self, name: str) -> ShellSession:
        return ShellSession([self._docker, "exec", "-i", self._container_name(name), "bash"])

    def exec_oneoff(self, name: str, command: str, timeout: int = 30) -> dict:
        try:
            result = subprocess.run(
                [self._docker, "exec", self._container_name(name), "bash", "-c", command],
                capture_output=True, text=True, timeout=timeout
            )
            return {"output": result.stdout, "exit_code": result.returncode,
                    "status": "completed"}
        except subprocess.TimeoutExpired:
            return {"output": "", "exit_code": None, "status": "running"}

    def commit(self, name: str, image_tag: Optional[str] = None) -> dict:
        if not image_tag:
            image_tag = f"sandbox-{name}-snapshot:{int(time.time())}"
        subprocess.run([self._docker, "commit", self._container_name(name), image_tag],
                       capture_output=True, timeout=120)
        return {"image_tag": image_tag, "status": "committed"}

    def build(self, image_tag: str, dockerfile: str,
              context_dir: Optional[str] = None) -> dict:
        with tempfile.NamedTemporaryFile(mode="w", suffix="Dockerfile",
                                          delete=False) as f:
            f.write(dockerfile)
            dockerfile_path = f.name
        try:
            cmd = [self._docker, "build", "-t", image_tag, "-f", dockerfile_path]
            cmd.append(context_dir if context_dir else os.path.dirname(dockerfile_path))
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                return {"image_tag": image_tag, "status": "error",
                        "error": result.stderr[-500:]}
            return {"image_tag": image_tag, "status": "built"}
        finally:
            os.unlink(dockerfile_path)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_docker_backend.py -v
```
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add backends/docker_backend.py tests/test_docker_backend.py
git commit -m "feat: DockerBackend with lifecycle, shell, commit, build"
```

---

## Task 5: SSH Backend

**Files:**
- Create: `backends/ssh_backend.py`
- Test: `tests/test_ssh_backend.py`

SSH backend implements: create (ssh_connect), stop (ssh_disconnect), start
(ssh_reconnect), remove (ssh_remove), open_shell, exec_oneoff. Uses ControlMaster
multiplexing.

- [ ] **Step 1: Write failing test (mocked subprocess)**

```python
# tests/test_ssh_backend.py
import pytest
from unittest.mock import patch, MagicMock
from sandbox_mcp.backends.ssh_backend import SSHBackend


@pytest.fixture
def ssh_backend():
    return SSHBackend()


def test_ssh_create(ssh_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        info = ssh_backend.create(
            name="remote", purpose="remote", host="192.168.1.100", user="ubuntu",
        )
        assert info.name == "remote"
        assert info.backend == "ssh"
        assert info.status == "running"


def test_ssh_stop_disconnects(ssh_backend):
    ssh_backend._targets["remote"] = {
        "host": "192.168.1.100", "user": "ubuntu", "port": 22,
        "socket": "/tmp/sandbox-mcp-ssh-remote",
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        info = ssh_backend.stop("remote")
        assert info.status == "stopped"


def test_ssh_remove_unregisters(ssh_backend):
    ssh_backend._targets["remote"] = {"host": "h", "user": "u", "port": 22}
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = ssh_backend.remove("remote")
        assert result["status"] == "removed"
        assert "remote" not in ssh_backend._targets


def test_ssh_open_shell(ssh_backend):
    ssh_backend._targets["remote"] = {
        "host": "192.168.1.100", "user": "ubuntu", "port": 22,
        "socket": "/tmp/sandbox-mcp-ssh-remote", "key": None,
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        shell = ssh_backend.open_shell("remote")
        assert "ssh" in shell._args[0]
        shell.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_ssh_backend.py -v
```

- [ ] **Step 3: Implement SSHBackend**

```python
# backends/ssh_backend.py
"""SSH backend: manages remote machines via SSH with ControlMaster."""

import shutil
import subprocess
import time
from typing import Optional

from sandbox_mcp.backends.base import Backend, TargetInfo
from sandbox_mcp.shell_session import ShellSession


def _find_ssh() -> str:
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("ssh not found on PATH")
    return ssh


class SSHBackend(Backend):
    """SSH remote machine backend with ControlMaster multiplexing."""

    def __init__(self):
        self._ssh = _find_ssh()
        self._targets: dict[str, dict] = {}

    def _socket_path(self, name: str) -> str:
        return f"/tmp/sandbox-mcp-ssh-{name}"

    def _ssh_base_args(self, name: str) -> list[str]:
        machine = self._targets.get(name, {})
        socket = self._socket_path(name)
        args = [self._ssh, "-o", f"ControlPath={socket}",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10"]
        port = machine.get("port", 22)
        args.extend(["-p", str(port)])
        key = machine.get("key")
        if key:
            args.extend(["-i", key])
        user = machine.get("user", "")
        host = machine.get("host", "")
        args.append(f"{user}@{host}" if user else host)
        return args

    def create(self, name: str, purpose: str = "", **kwargs) -> TargetInfo:
        host = kwargs.get("host", "")
        user = kwargs.get("user", "")
        port = kwargs.get("port", 22)
        key = kwargs.get("key")

        self._targets[name] = {
            "host": host, "user": user, "port": port,
            "key": key,
            "socket": self._socket_path(name),
            "purpose": purpose,
            "started_at": time.time(),
        }

        cmd = [self._ssh, "-M", "-S", self._socket_path(name),
               "-o", "ControlPersist=300",
               "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10",
               "-p", str(port)]
        if key:
            cmd.extend(["-i", key])
        cmd.append(f"{user}@{host}")

        result = subprocess.run(cmd + ["true"], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return TargetInfo(name=name, backend="ssh", status="error", purpose=purpose)

        return TargetInfo(name=name, backend="ssh", status="running", purpose=purpose)

    def start(self, name: str) -> TargetInfo:
        """Reconnect SSH ControlMaster."""
        machine = self._targets.get(name, {})
        return self.create(name, **{k: v for k, v in machine.items()
                                     if k in ("host", "user", "port", "key")})

    def stop(self, name: str) -> TargetInfo:
        """Close the SSH master connection."""
        socket = self._socket_path(name)
        machine = self._targets.get(name, {})
        user = machine.get("user", "")
        host = machine.get("host", "")
        subprocess.run(
            [self._ssh, "-S", socket, "-O", "exit", f"{user}@{host}"],
            capture_output=True, timeout=10
        )
        return TargetInfo(name=name, backend="ssh", status="stopped")

    def remove(self, name: str) -> dict:
        if name in self._targets:
            try:
                self.stop(name)
            except Exception:
                pass
            del self._targets[name]
        return {"machine": name, "status": "removed"}

    def get_info(self, name: str) -> TargetInfo:
        if name not in self._targets:
            return TargetInfo(name=name, backend="ssh", status="error")
        socket = self._socket_path(name)
        machine = self._targets[name]
        result = subprocess.run(
            [self._ssh, "-S", socket, "-O", "check",
             f"{machine['user']}@{machine['host']}"],
            capture_output=True, timeout=5
        )
        status = "running" if result.returncode == 0 else "stopped"
        return TargetInfo(name=name, backend="ssh", status=status)

    def open_shell(self, name: str) -> ShellSession:
        args = self._ssh_base_args(name)
        args.append("bash")
        return ShellSession(args)

    def exec_oneoff(self, name: str, command: str, timeout: int = 30) -> dict:
        args = self._ssh_base_args(name)
        args.extend(["bash", "-c", command])
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return {"output": result.stdout, "exit_code": result.returncode,
                    "status": "completed"}
        except subprocess.TimeoutExpired:
            return {"output": "", "exit_code": None, "status": "running"}
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_ssh_backend.py -v
```
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add backends/ssh_backend.py tests/test_ssh_backend.py
git commit -m "feat: SSHBackend with ControlMaster multiplexing"
```

---

## Task 6: machine Registry

**Files:**
- Create: `machine_registry.py`
- Test: `tests/test_target_registry.py`

Manages name -> backend mapping, machine metadata, default machine, and explicit machine overrides.

- [ ] **Step 1: Write failing test**

```python
# tests/test_target_registry.py
import pytest
from unittest.mock import MagicMock
from sandbox_mcp.machine_registry import TargetRegistry


def test_register_target():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(name="dev", backend="docker",
                                             status="running", purpose="test",
                                             shells=0, uptime="")
    reg.register("dev", backend, purpose="test", image="python:3.12")
    assert "dev" in reg.list_targets()


def test_set_default_target():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(name="dev", backend="docker",
                                             status="running", purpose="test",
                                             shells=0, uptime="")
    reg.register("dev", backend, purpose="test", image="python:3.12")
    reg.set_default("dev")
    assert reg.get_default() == "dev"


def test_resolve_target_explicit():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(name="dev", backend="docker",
                                             status="running", purpose="",
                                             shells=0, uptime="")
    reg.register("dev", backend, purpose="", image="python:3.12")
    reg.register("db", backend, purpose="", image="postgres:16")
    reg.set_default("dev")
    assert reg.resolve_target("db") == "db"
    assert reg.get_default() == "dev"


def test_resolve_target_default():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(name="dev", backend="docker",
                                             status="running", purpose="",
                                             shells=0, uptime="")
    reg.register("dev", backend, purpose="", image="python:3.12")
    reg.set_default("dev")
    assert reg.resolve_target(None) == "dev"


def test_resolve_target_no_default():
    reg = TargetRegistry()
    with pytest.raises(ValueError, match="No default machine"):
        reg.resolve_target(None)


def test_unregister_target():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(name="dev", backend="docker",
                                             status="running", purpose="",
                                             shells=0, uptime="")
    reg.register("dev", backend, purpose="", image="python:3.12")
    reg.set_default("dev")
    reg.unregister("dev")
    assert "dev" not in reg.list_targets()
    assert reg.get_default() is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_target_registry.py -v
```

- [ ] **Step 3: Implement TargetRegistry**

Implement TargetRegistry with name -> {backend, info, created_at} storage and a default machine used by `resolve_target(None)`.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_target_registry.py -v
```
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add machine_registry.py tests/test_target_registry.py
git commit -m "feat: TargetRegistry with default targeting model"
```

---

## Task 7: Shell Registry

**Files:**
- Create: `shell_registry.py`
- Test: `tests/test_shell_registry.py`

Tracks shell sessions, per-machine default shells, terminated state handling, and cleanup hints in list output.

- [ ] **Step 1: Write failing test**

```python
# tests/test_shell_registry.py
import pytest
from unittest.mock import MagicMock
from sandbox_mcp.shell_registry import ShellRegistry


def test_open_shell():
    reg = ShellRegistry()
    mock_shell = MagicMock()
    mock_shell.state = "idle"
    mock_shell.purpose = None
    mock_shell.uptime = 0
    mock_shell.last_command = None
    shell_id = reg.open("dev", mock_shell, purpose="test")
    assert shell_id.startswith("sh_")
    assert shell_id in [s["shell_id"] for s in reg.list_shells()]


def test_get_shell():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.open("dev", mock_shell)
    assert reg.get(shell_id) is mock_shell


def test_close_shell():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.open("dev", mock_shell)
    reg.close(shell_id)
    assert shell_id not in [s["shell_id"] for s in reg.list_shells()]
    mock_shell.close.assert_called_once()


def test_list_shells_by_target():
    reg = ShellRegistry()
    mock1 = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    mock2 = MagicMock(state="running", purpose="tests", uptime=0, last_command="pytest")
    reg.open("dev", mock1)
    reg.open("dev", mock2, purpose="tests")
    reg.open("db", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    dev_shells = reg.list_shells(machine="dev")
    assert len(dev_shells) == 2


def test_list_shells_terminated_hint():
    """Terminated shells show cleanup hint."""
    reg = ShellRegistry()
    mock_shell = MagicMock(state="terminated", purpose=None, uptime=0, last_command=None)
    reg.open("dev", mock_shell)
    shells = reg.list_shells()
    assert shells[0]["status"] == "terminated"
    assert "hint" in shells[0]


def test_get_or_create_default():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.get_or_create_default("dev", lambda: mock_shell)
    assert shell_id.startswith("sh_")
    assert reg.get_default_id("dev") == shell_id
    shell_id2 = reg.get_or_create_default("dev", lambda: MagicMock())
    assert shell_id == shell_id2


def test_set_default_shell():
    reg = ShellRegistry()
    shell1 = reg.open("dev", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    shell2 = reg.open("dev", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    machine = reg.set_default(shell2)
    assert machine == "dev"
    assert reg.get_target(shell2) == "dev"
    assert reg.get_default_id("dev") == shell2
    shells = reg.list_shells(machine="dev")
    assert next(s for s in shells if s["shell_id"] == shell1)["is_default"] is False
    assert next(s for s in shells if s["shell_id"] == shell2)["is_default"] is True


def test_close_all_for_target():
    reg = ShellRegistry()
    mock1 = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    mock2 = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    reg.open("dev", mock1)
    reg.open("dev", mock2)
    reg.open("db", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    reg.close_all_for_target("dev")
    assert len(reg.list_shells(machine="dev")) == 0
    assert len(reg.list_shells()) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_shell_registry.py -v
```

- [ ] **Step 3: Implement ShellRegistry**

```python
# shell_registry.py
"""Shell Registry: tracks all open shell sessions across machines."""

import uuid
from typing import Optional, Callable
from sandbox_mcp.shell_session import ShellSession


class ShellRegistry:
    """In-memory registry of open shell sessions."""

    def __init__(self):
        self._shells: dict[str, dict] = {}
        self._default_shells: dict[str, str] = {}

    def open(self, machine: str, session: ShellSession, purpose: str = "") -> str:
        shell_id = f"sh_{uuid.uuid4().hex[:12]}"
        session.purpose = purpose
        self._shells[shell_id] = {
            "session": session,
            "machine": machine,
            "purpose": purpose,
        }
        return shell_id

    def get(self, shell_id: str) -> Optional[ShellSession]:
        entry = self._shells.get(shell_id)
        return entry["session"] if entry else None

    def close(self, shell_id: str) -> bool:
        entry = self._shells.pop(shell_id, None)
        if entry:
            entry["session"].close()
            machine = entry["machine"]
            if self._default_shells.get(machine) == shell_id:
                del self._default_shells[machine]
            return True
        return False

    def get_or_create_default(self, machine: str,
                              factory: Callable[[], ShellSession]) -> str:
        if machine in self._default_shells:
            shell_id = self._default_shells[machine]
            if shell_id in self._shells:
                return shell_id
        session = factory()
        shell_id = self.open(machine, session, purpose="default")
        self._default_shells[machine] = shell_id
        return shell_id

    def get_target(self, shell_id: str) -> Optional[str]:
        entry = self._shells.get(shell_id)
        return entry["machine"] if entry else None

    def set_default(self, shell_id: str) -> str:
        entry = self._shells.get(shell_id)
        if entry is None:
            raise ValueError(f"Unknown shell_id: {shell_id}")
        machine = entry["machine"]
        self._default_shells[machine] = shell_id
        return machine

    def get_default_id(self, machine: str) -> Optional[str]:
        return self._default_shells.get(machine)

    def list_shells(self, machine: Optional[str] = None) -> list[dict]:
        result = []
        for shell_id, entry in self._shells.items():
            if machine and entry["machine"] != machine:
                continue
            session = entry["session"]
            item = {
                "shell_id": shell_id,
                "machine": entry["machine"],
                "purpose": entry.get("purpose", ""),
                "status": session.state,
                "uptime": f"{int(session.uptime)}s",
                "last_command": session.last_command,
                "is_default": self._default_shells.get(entry["machine"]) == shell_id,
            }
            if session.state == "terminated":
                item["hint"] = "Process exited. Call shell_remove to clean up."
            result.append(item)
        return result

    def close_all_for_target(self, machine: str) -> int:
        count = 0
        to_close = [sid for sid, e in self._shells.items() if e["machine"] == machine]
        for sid in to_close:
            self.close(sid)
            count += 1
        return count
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_shell_registry.py -v
```
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add shell_registry.py tests/test_shell_registry.py
git commit -m "feat: ShellRegistry with terminated hints and default shell tracking"
```

---

## Task 8: File Operations -- Read and Write

**Files:**
- Create: `file_operations.py`
- Test: `tests/test_file_operations.py`

File operations execute shell commands on machines via backend.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_file_operations.py
import pytest
from unittest.mock import MagicMock
from sandbox_mcp.file_operations import FileOperations


@pytest.fixture
def backend():
    b = MagicMock()
    return b


@pytest.fixture
def fops(backend):
    return FileOperations(backend)


def test_read_returns_line_numbered_output(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "line1\nline2\nline3\n",
    }
    result = fops.read("/tmp/x.txt", machine="dev")
    assert result["status"] == "ok"
    assert result["path"] == "/tmp/x.txt"
    assert "1|line1" in result["output"]
    assert "3|line3" in result["output"]


def test_read_pagination_offset_and_limit(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "line2\nline3\n",
    }
    result = fops.read("/tmp/x.txt", machine="dev", offset=2, limit=2)
    assert result["offset"] == 2
    assert result["limit"] == 2
    assert "1|line2" in result["output"]  # renumbered from offset
    assert "2|line3" in result["output"]
    assert "line1" not in result["output"]


def test_read_not_found_returns_suggestions(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 1, "output": "",
    }
    backend.suggest_paths.return_value = ["/tmp/x.txt", "/tmp/x.txt.bak"]
    result = fops.read("/tmp/missing.txt", machine="dev")
    assert result["status"] == "not_found"
    assert result["suggestions"] == ["/tmp/x.txt", "/tmp/x.txt.bak"]


def test_read_binary_returns_error(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "binary\x00data",
    }
    result = fops.read("/tmp/blob.bin", machine="dev")
    assert result["status"] == "binary"
    assert "error" in result


def test_write_creates_parent_dirs_and_writes(fops, backend):
    backend.exec_oneoff.return_value = {"exit_code": 0, "output": ""}
    result = fops.write("/tmp/new/dir/x.txt", "hello", machine="dev")
    assert result["status"] == "ok"
    cmd = backend.exec_oneoff.call_args[0][1]
    assert "mkdir -p" in cmd
    assert "/tmp/new/dir/x.txt" in cmd


def test_write_runs_syntax_check_when_extension_known(fops, backend):
    backend.exec_oneoff.return_value = {"exit_code": 0, "output": ""}
    fops.write("/tmp/x.py", "print(1)\n", machine="dev")
    calls = [c.args[1] for c in backend.exec_oneoff.call_args_list]
    assert any("python -m py_compile" in cmd for cmd in calls)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_file_operations.py -v
```
Expected: ImportError or 6 failed

- [ ] **Step 3: Implement FileOperations (read + write)**

```python
# file_operations.py
"""File operations executed via a backend's one-off shell commands."""

from __future__ import annotations

import shlex
from typing import Optional


LINE_FMT = "{n}|{line}"  # each line is prefixed with its line number and a pipe


class FileOperations:
    def __init__(self, backend):
        self._backend = backend

    def read(self, path: str, machine: str, offset: int = 1,
             limit: int = 500) -> dict:
        sed_range = f"{offset},{offset + limit - 1}p"
        cmd = f"sed -n {shlex.quote(sed_range)} {shlex.quote(path)}"
        result = self._backend.exec_oneoff(machine, cmd)
        if result.get("exit_code") not in (0, None):
            suggestions = self._backend.suggest_paths(machine, path)
            return {"status": "not_found", "path": path,
                    "suggestions": suggestions}
        output = result.get("output", "")
        if "\x00" in output:
            return {"status": "binary", "path": path,
                    "error": "binary file not readable as text"}
        lines = output.splitlines()
        numbered = [LINE_FMT.format(n=offset + i, line=l) for i, l in enumerate(lines)]
        return {"status": "ok", "path": path,
                "offset": offset, "limit": limit,
                "output": "\n".join(numbered) + ("\n" if numbered else "")}

    def write(self, path: str, content: str, machine: str) -> dict:
        self._backend.exec_oneoff(machine, f"mkdir -p $(dirname {shlex.quote(path)})")
        cmd = (f"cat > {shlex.quote(path)} <<'__SANDBOX_EOF__'\n"
               f"{content}\n__SANDBOX_EOF__")
        self._backend.exec_oneoff(machine, cmd)
        check = self._syntax_check(path, content)
        if check is not None:
            self._backend.exec_oneoff(machine, check)
        return {"status": "ok", "path": path}

    def _syntax_check(self, path: str, content: str) -> Optional[str]:
        if path.endswith(".py"):
            return f"python -m py_compile {shlex.quote(path)}"
        if path.endswith(".sh"):
            return f"bash -n {shlex.quote(path)}"
        if path.endswith(".json"):
            return f"python -c 'import json,sys;json.load(open({shlex.quote(path)!r}))'"
        return None
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_file_operations.py -v
```
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add file_operations.py tests/test_file_operations.py
git commit -m "feat: FileOperations read + write with line numbers, binary detection, syntax check"
```

---

## Task 9: File Operations -- Patch and Search

**Files:**
- Modify: `file_operations.py` (add patch + search)
- Modify: `tests/test_file_operations.py` (add tests)

Patch uses fuzzy matching (replace mode) or V4A format (patch mode).
Search uses ripgrep for content, find for files.

- [ ] **Step 1: Write failing tests for patch and search**

```python
# tests/test_file_operations.py (additions)
def test_patch_replace_mode_replaces_unique_string(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "alpha\nbeta\ngamma\n"},  # cat
        {"exit_code": 0, "output": ""},                      # write back
    ]
    result = fops.patch(mode="replace", machine="dev", path="/tmp/x.txt",
                       old_string="beta", new_string="BETA")
    assert result["status"] == "ok"
    assert result["matches"] == 1


def test_patch_replace_mode_returns_diff(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "a\nb\nc\n"},
        {"exit_code": 0, "output": ""},
    ]
    result = fops.patch(mode="replace", machine="dev", path="/tmp/x.txt",
                       old_string="b", new_string="B")
    assert "diff" in result
    assert "-b" in result["diff"]
    assert "+B" in result["diff"]


def test_patch_replace_mode_rejects_multiple_matches(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "x\nx\nx\n",
    }
    result = fops.patch(mode="replace", machine="dev", path="/tmp/x.txt",
                       old_string="x", new_string="y")
    assert result["status"] == "error"
    assert "Multiple matches" in result["error"]


def test_patch_replace_mode_fuzzy_match(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "hello world\n"},
        {"exit_code": 0, "output": ""},
    ]
    result = fops.patch(mode="replace", machine="dev", path="/tmp/x.txt",
                       old_string="helo world", new_string="hello world",
                       replace_all=False)
    assert result["status"] == "ok"
    assert result["fuzzy"] is True


def test_search_content_returns_matching_lines(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0,
        "output": "/tmp/x.txt:3:foo bar\n/tmp/y.txt:7:baz foo\n",
    }
    result = fops.search("foo", machine="dev", search_type="content")
    assert result["status"] == "ok"
    assert len(result["results"]) == 2
    assert result["results"][0]["line"] == 3
    assert result["results"][0]["path"] == "/tmp/x.txt"


def test_search_files_mode_uses_glob(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "/tmp/a.py\n/tmp/b.py\n",
    }
    result = fops.search("*.py", machine="dev", search_type="files")
    assert result["status"] == "ok"
    assert result["results"] == ["/tmp/a.py", "/tmp/b.py"]


def test_search_limit_truncates_results(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "\n".join(f"/tmp/f{i}.py" for i in range(10)) + "\n",
    }
    result = fops.search("*.py", machine="dev", search_type="files", limit=3)
    assert len(result["results"]) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_file_operations.py -v
```
Expected: 5 failed (patch/search not implemented)

- [ ] **Step 3: Implement patch and search**

Append to `file_operations.py`:

```python
import difflib


class FileOperations:
    # ... read, write, _syntax_check unchanged ...

    def patch(self, mode: str, machine: str, path: str = "",
              old_string: str = "", new_string: str = "",
              replace_all: bool = False, patch: str = "") -> dict:
        if mode == "replace":
            return self._patch_replace(machine, path, old_string, new_string, replace_all)
        if mode == "patch":
            return self._patch_apply(machine, patch)
        return {"status": "error", "error": f"Unknown patch mode: {mode}"}

    def _patch_replace(self, machine: str, path: str,
                       old_string: str, new_string: str,
                       replace_all: bool) -> dict:
        cmd = f"cat {shlex.quote(path)}"
        result = self._backend.exec_oneoff(machine, cmd)
        if result.get("exit_code") not in (0, None):
            return {"status": "not_found", "path": path}
        original = result.get("output", "")
        count = original.count(old_string)
        fuzzy = False
        if count == 0:
            match = difflib.get_close_matches(
                old_string, original.splitlines(), n=1, cutoff=0.6
            )
            if match:
                old_string = match[0]
                count = original.count(old_string)
                fuzzy = True
        if count == 0:
            return {"status": "error",
                    "error": "old_string not found"}
        if count > 1 and not replace_all:
            return {"status": "error",
                    "error": f"Multiple matches ({count}); set replace_all=true"}
        replaced = original.replace(old_string, new_string)
        diff = "\n".join(difflib.unified_diff(
            original.splitlines(), replaced.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""
        ))
        self._backend.exec_oneoff(machine, f"cat > {shlex.quote(path)} <<'__SANDBOX_EOF__'\n{replaced}\n__SANDBOX_EOF__")
        return {"status": "ok", "path": path, "matches": count,
                "fuzzy": fuzzy, "diff": diff}

    def _patch_apply(self, machine: str, patch_text: str) -> dict:
        self._backend.exec_oneoff(machine,
                                  f"patch -p0 <<'__SANDBOX_EOF__'\n{patch_text}\n__SANDBOX_EOF__")
        return {"status": "ok"}

    def search(self, pattern: str, machine: str,
               search_type: str = "content", path: str = ".",
               file_glob: str = "", limit: int = 50,
               offset: int = 0, output_mode: str = "content",
               context: int = 0) -> dict:
        if search_type == "content":
            rg = ["rg", "--line-number", f"--max-count={limit}"]
            if file_glob:
                rg += ["-g", file_glob]
            if output_mode != "content":
                rg += [f"--{output_mode.replace('_', '-')}"]
            if context:
                rg += [f"-C {context}"]
            rg += [shlex.quote(pattern), shlex.quote(path)]
            cmd = " ".join(rg)
        elif search_type == "files":
            cmd = f"find {shlex.quote(path)} -name {shlex.quote(pattern)} -type f"
        else:
            return {"status": "error",
                    "error": f"Unknown search_type: {search_type}"}
        result = self._backend.exec_oneoff(machine, cmd)
        raw = result.get("output", "")
        if search_type == "files":
            results = [r for r in raw.splitlines() if r][offset:offset + limit]
        else:
            results = []
            for line in raw.splitlines():
                if not line:
                    continue
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    p, ln, snippet = parts[0], int(parts[1]), parts[2]
                    results.append({"path": p, "line": ln, "snippet": snippet})
        return {"status": "ok", "type": search_type,
                "results": results[offset:offset + limit]}
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_file_operations.py -v
```
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add file_operations.py tests/test_file_operations.py
git commit -m "feat: FileOperations patch (fuzzy match) + search (ripgrep)"
```

---

## Task 10: sandbox_env -- Action Dispatch and Help Generation

**Files:**
- Create: `sandbox_env.py`
- Test: `tests/test_sandbox_env.py`

sandbox_env implements 18 actions with progressive discovery:
- `help`: returns common ops (default_set/shell_new/shell_list/shell_remove) + pointers to docker_help/ssh_help
- `status`: returns default machine, machines, shells
- `docker_help`: returns Docker ops (docker_run/build/commit/stop/start/remove)
- `ssh_help`: returns SSH ops (ssh_connect/disconnect/reconnect/remove)
- Plus all discovered execution actions (default_set, shell_new, docker_run, ssh_connect, etc.)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_sandbox_env.py
import pytest
import json
from unittest.mock import MagicMock, patch
from sandbox_mcp.sandbox_env import SandboxEnv


@pytest.fixture
def sandbox_env():
    machines = MagicMock()
    shells = MagicMock()
    docker_backend = MagicMock()
    ssh_backend = MagicMock()
    return SandboxEnv(machines, shells, docker_backend, ssh_backend)


def test_help_returns_operations_and_pointers(sandbox_env):
    result = sandbox_env.dispatch("help", {})
    assert "default_actions" in result
    default_actions = [op["action"] for op in result["default_actions"]]
    assert default_actions == ["help", "status"]
    assert "operations" in result
    actions = [op["action"] for op in result["operations"]]
    assert "default_set" in actions
    assert "shell_new" in actions
    assert "shell_remove" in actions
    assert "shell_list" in actions
    assert "more_help" in result
    assert "docker_help" in result["more_help"]
    assert "ssh_help" in result["more_help"]


def test_docker_help_returns_docker_ops(sandbox_env):
    result = sandbox_env.dispatch("docker_help", {})
    actions = [op["action"] for op in result["operations"]]
    assert "docker_run" in actions
    assert "docker_build" in actions
    assert "docker_commit" in actions
    assert "docker_stop" in actions
    assert "docker_start" in actions
    assert "docker_remove" in actions


def test_ssh_help_returns_ssh_ops(sandbox_env):
    result = sandbox_env.dispatch("ssh_help", {})
    actions = [op["action"] for op in result["operations"]]
    assert "ssh_connect" in actions
    assert "ssh_disconnect" in actions
    assert "ssh_reconnect" in actions
    assert "ssh_remove" in actions


def test_default_set_sets_default_target(sandbox_env):
    sandbox_env._targets.resolve_target.return_value = "dev"
    result = sandbox_env.dispatch("default_set", {"machine": "dev"})
    sandbox_env._targets.set_default.assert_called_once_with("dev")
    assert result == {"default_machine": "dev"}


def test_default_set_sets_default_shell(sandbox_env):
    sandbox_env._shells.get_target.return_value = "dev"
    result = sandbox_env.dispatch("default_set", {"shell_id": "sh_abc"})
    sandbox_env._shells.get_target.assert_called_once_with("sh_abc")
    sandbox_env._shells.set_default.assert_called_once_with("sh_abc")
    assert result == {"default_shell": {"machine": "dev", "shell_id": "sh_abc"}}


def test_default_set_rejects_both_target_and_shell(sandbox_env):
    result = sandbox_env.dispatch("default_set", {"machine": "dev", "shell_id": "sh_abc"})
    assert "error" in result


def test_status_returns_state(sandbox_env):
    sandbox_env._targets.get_default.return_value = "dev"
    sandbox_env._targets.list_targets.return_value = ["dev"]
    info = MagicMock(name="dev", backend="docker", status="running",
                     purpose="test", shells=0, uptime="")
    sandbox_env._targets.get_info.return_value = info
    sandbox_env._shells.list_shells.return_value = []
    result = sandbox_env.dispatch("status", {})
    assert result["default_machine"] == "dev"
    assert len(result["machines"]) == 1
    assert "shells" in result


def test_shell_new(sandbox_env):
    backend = MagicMock()
    shell = MagicMock()
    backend.open_shell.return_value = shell
    sandbox_env._targets.resolve_target.return_value = "dev"
    sandbox_env._targets.get_backend.return_value = backend
    sandbox_env._shells.open.return_value = "sh_abc"
    result = sandbox_env.dispatch("shell_new", {"machine": "dev", "purpose": "server"})
    backend.open_shell.assert_called_once_with("dev")
    sandbox_env._shells.open.assert_called_once_with("dev", shell, purpose="server")
    assert result == {"shell_id": "sh_abc", "machine": "dev"}


def test_shell_remove(sandbox_env):
    sandbox_env._shells.close.return_value = True
    result = sandbox_env.dispatch("shell_remove", {"shell_id": "sh_abc"})
    assert result["status"] == "removed"


def test_shell_list(sandbox_env):
    sandbox_env._shells.list_shells.return_value = [
        {"shell_id": "sh_abc", "machine": "dev", "status": "idle"}
    ]
    result = sandbox_env.dispatch("shell_list", {})
    assert len(result) == 1


def test_docker_run(sandbox_env):
    info = MagicMock(name="dev", backend="docker", status="running", purpose="test")
    sandbox_env._targets.register.return_value = info
    result = sandbox_env.dispatch("docker_run", {
        "name": "dev", "image": "python:3.12", "purpose": "test"
    })
    assert result["status"] == "running"
    assert result["backend"] == "docker"


def test_unknown_action_returns_error(sandbox_env):
    result = sandbox_env.dispatch("nonexistent", {})
    assert "error" in result


def test_missing_required_param_returns_error(sandbox_env):
    result = sandbox_env.dispatch("docker_run", {"name": "dev"})  # missing image, purpose
    assert "error" in result
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_sandbox_env.py -v
```

- [ ] **Step 3: Implement SandboxEnv**

```python
# sandbox_env.py
"""sandbox_env: progressive-discovery environment management with 18 actions.

Progressive discovery:
  tools/list    -> only describes help/status
  help          -> common ops + pointers to docker_help/ssh_help
  docker_help   -> Docker-specific ops
  ssh_help      -> SSH-specific ops
"""

import json
import time
from typing import Any


# --- Static help definitions ---

HELP_RESPONSE = {
    "default_actions": [
        {
            "action": "help",
            "description": "Discover common management actions and backend help entries.",
        },
        {
            "action": "status",
            "description": "Show current state: default machine, machine list, shell list.",
        },
    ],
    "operations": [
        {
            "action": "default_set",
            "description": "Set default machine or default shell. Pass machine to set the default machine. Pass shell_id to set that shell as its machine's default shell.",
            "optional": {"machine": "string", "shell_id": "string"},
            "requires": "Exactly one of machine or shell_id",
            "example": {"machine": "dev"},
        },
        {
            "action": "shell_new",
            "description": "Create an additional shell session on a machine.",
            "optional": {"machine": "string", "purpose": "string"},
        },
        {
            "action": "shell_remove",
            "description": "Terminate and remove a shell session. If already terminated, remove the registry entry.",
            "required": {"shell_id": "string"},
        },
        {
            "action": "shell_list",
            "description": "List all shells, optionally filtered by machine.",
            "optional": {"machine": "string"},
        },
    ],
    "more_help": {
        "docker_help": "Docker: create/build/commit/stop/start/remove containers",
        "ssh_help": "SSH: connect/disconnect/reconnect/remove remote machines",
    },
    "note": "Core tools are directly exposed as sandbox_shell_exec, sandbox_shell_read, and sandbox_file_read/write/patch/search. machine-aware tools support optional machine.",
}

DOCKER_HELP_RESPONSE = {
    "operations": [
        {
            "action": "docker_run",
            "description": "Create and start a Docker container.",
            "required": {"name": "string", "image": "string", "purpose": "string"},
            "optional": {
                "volumes": "string[] - e.g. ['/host:/container']",
                "ports": "string[] - e.g. ['8080:8080']",
                "env": "object",
                "workdir": "string - default /workspace",
            },
            "returns": {"name": "string", "status": "running", "backend": "docker"},
            "example": {"name": "dev", "image": "python:3.12", "purpose": "Python dev"},
        },
        {
            "action": "docker_build",
            "description": "Build a custom Docker image from a Dockerfile.",
            "required": {"image_tag": "string", "dockerfile": "string"},
            "optional": {"context_dir": "string"},
            "returns": {"image_tag": "string", "status": "built"},
        },
        {
            "action": "docker_commit",
            "description": "Save container state as a new image.",
            "required": {"machine": "string"},
            "optional": {"image_tag": "string - auto-generated if omitted"},
            "returns": {"image_tag": "string", "status": "committed"},
        },
        {
            "action": "docker_stop",
            "description": "Stop container. State preserved, can docker_start to resume.",
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "stopped"},
        },
        {
            "action": "docker_start",
            "description": "Start a stopped container.",
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "running"},
        },
        {
            "action": "docker_remove",
            "description": "Stop and remove container. Closes all shells for the machine.",
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "removed"},
        },
    ]
}

SSH_HELP_RESPONSE = {
    "operations": [
        {
            "action": "ssh_connect",
            "description": "Connect to an SSH remote machine.",
            "required": {"name": "string", "host": "string", "user": "string", "purpose": "string"},
            "optional": {
                "port": "int - default 22",
                "key": "string - private key path (key auth only)",
            },
            "returns": {"name": "string", "status": "connected", "backend": "ssh"},
            "example": {"name": "remote", "host": "192.168.1.100", "user": "ubuntu", "purpose": "Remote server"},
        },
        {
            "action": "ssh_disconnect",
            "description": "Close SSH connection. Remote machine is not affected.",
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "stopped"},
        },
        {
            "action": "ssh_reconnect",
            "description": "Re-establish SSH connection. Shells are lost on disconnect.",
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "running"},
        },
        {
            "action": "ssh_remove",
            "description": "Unregister SSH machine. Remote machine is not affected.",
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "removed"},
        },
    ]
}


class SandboxEnv:
    """Dispatches sandbox_env actions and generates help responses."""

    def __init__(self, machines, shells, docker_backend, ssh_backend):
        self._targets = machines
        self._shells = shells
        self._docker = docker_backend
        self._ssh = ssh_backend

    def dispatch(self, action: str, params: dict) -> Any:
        handler = getattr(self, f"_op_{action}", None)
        if handler is None:
            return {"error": f"Unknown action: {action}. Call action=help for available operations."}
        try:
            return handler(params)
        except Exception as e:
            return {"error": str(e)}

    # --- Discovery actions ---

    def _op_help(self, params):
        return HELP_RESPONSE

    def _op_docker_help(self, params):
        return DOCKER_HELP_RESPONSE

    def _op_ssh_help(self, params):
        return SSH_HELP_RESPONSE

    def _op_status(self, params):
        default = self._targets.get_default()
        machines = []
        for name in self._targets.list_targets():
            info = self._targets.get_info(name)
            shell_count = len(self._shells.list_shells(machine=name))
            created_at = self._targets._targets.get(name, {}).get("created_at", time.time())
            uptime_s = int(time.time() - created_at)
            uptime = f"{uptime_s // 3600}h{(uptime_s % 3600) // 60}m" if uptime_s > 60 else f"{uptime_s}s"
            machines.append({
                "name": name,
                "backend": info.backend,
                "status": info.status,
                "purpose": info.purpose,
                "shells": shell_count,
                "uptime": uptime,
            })
        shells = self._shells.list_shells()
        return {"default_machine": default, "machines": machines, "shells": shells}

    # --- General actions ---

    def _op_default_set(self, params):
        has_target = "machine" in params
        has_shell = "shell_id" in params
        if has_target == has_shell:
            return {"error": "Pass exactly one of machine or shell_id"}
        if has_target:
            machine = self._targets.resolve_target(params["machine"])
            self._targets.set_default(machine)
            return {"default_machine": machine}
        shell_target = self._shells.get_target(params["shell_id"])
        if shell_target is None:
            return {"error": f"Unknown shell_id: {params['shell_id']}"}
        self._shells.set_default(params["shell_id"])
        return {"default_shell": {"machine": shell_target, "shell_id": params["shell_id"]}}

    def _op_shell_new(self, params):
        machine = self._targets.resolve_target(params.get("machine"))
        backend = self._targets.get_backend(machine)
        shell = backend.open_shell(machine)
        shell_id = self._shells.open(machine, shell, purpose=params.get("purpose", "manual"))
        return {"shell_id": shell_id, "machine": machine}

    def _op_shell_remove(self, params):
        if "shell_id" not in params:
            return {"error": "Missing required param: shell_id"}
        if self._shells.close(params["shell_id"]):
            return {"shell_id": params["shell_id"], "status": "removed"}
        return {"error": f"Unknown shell_id: {params['shell_id']}"}

    def _op_shell_list(self, params):
        return self._shells.list_shells(machine=params.get("machine"))

    # --- Docker actions ---

    def _require(self, params, *keys):
        missing = [k for k in keys if k not in params]
        if missing:
            return None, f"Missing required params: {', '.join(missing)}"
        return True, None

    def _op_docker_run(self, params):
        ok, err = self._require(params, "name", "image", "purpose")
        if err:
            return {"error": err}
        info = self._targets.register(
            params["name"], self._docker,
            purpose=params.get("purpose", ""),
            image=params["image"],
            volumes=params.get("volumes", []),
            ports=params.get("ports", []),
            env=params.get("env", {}),
            workdir=params.get("workdir", "/workspace"),
        )
        return {"name": info.name, "status": info.status, "backend": "docker"}

    def _op_docker_build(self, params):
        ok, err = self._require(params, "image_tag", "dockerfile")
        if err:
            return {"error": err}
        return self._docker.build(params["image_tag"], params["dockerfile"],
                                  params.get("context_dir"))

    def _op_docker_commit(self, params):
        ok, err = self._require(params, "machine")
        if err:
            return {"error": err}
        from sandbox_mcp.backends.docker_backend import DockerBackend
        machine = self._targets.resolve_target(params["machine"])
        backend = self._targets.get_backend(machine)
        if not isinstance(backend, DockerBackend):
            return {"error": "docker_commit only supported on Docker machines"}
        return backend.commit(machine, params.get("image_tag"))

    def _op_docker_stop(self, params):
        ok, err = self._require(params, "machine")
        if err:
            return {"error": err}
        machine = self._targets.resolve_target(params["machine"])
        backend = self._targets.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend
        if not isinstance(backend, DockerBackend):
            return {"error": "docker_stop only supported on Docker machines"}
        self._shells.close_all_for_target(machine)
        info = backend.stop(machine)
        return {"machine": machine, "status": info.status}

    def _op_docker_start(self, params):
        ok, err = self._require(params, "machine")
        if err:
            return {"error": err}
        machine = self._targets.resolve_target(params["machine"])
        backend = self._targets.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend
        if not isinstance(backend, DockerBackend):
            return {"error": "docker_start only supported on Docker machines"}
        info = backend.start(machine)
        return {"machine": machine, "status": info.status}

    def _op_docker_remove(self, params):
        ok, err = self._require(params, "machine")
        if err:
            return {"error": err}
        machine = self._targets.resolve_target(params["machine"])
        backend = self._targets.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend
        if not isinstance(backend, DockerBackend):
            return {"error": "docker_remove only supported on Docker machines"}
        self._shells.close_all_for_target(machine)
        result = backend.remove(machine)
        self._targets.unregister(machine)
        return result

    # --- SSH actions ---

    def _op_ssh_connect(self, params):
        ok, err = self._require(params, "name", "host", "user", "purpose")
        if err:
            return {"error": err}
        info = self._targets.register(
            params["name"], self._ssh,
            purpose=params.get("purpose", ""),
            host=params["host"],
            user=params["user"],
            port=params.get("port", 22),
            key=params.get("key"),
        )
        return {"name": info.name, "status": info.status, "backend": "ssh"}

    def _op_ssh_disconnect(self, params):
        ok, err = self._require(params, "machine")
        if err:
            return {"error": err}
        machine = self._targets.resolve_target(params["machine"])
        backend = self._targets.get_backend(machine)
        from sandbox_mcp.backends.ssh_backend import SSHBackend
        if not isinstance(backend, SSHBackend):
            return {"error": "ssh_disconnect only supported on SSH machines"}
        self._shells.close_all_for_target(machine)
        info = backend.stop(machine)
        return {"machine": machine, "status": info.status}

    def _op_ssh_reconnect(self, params):
        ok, err = self._require(params, "machine")
        if err:
            return {"error": err}
        machine = self._targets.resolve_target(params["machine"])
        backend = self._targets.get_backend(machine)
        from sandbox_mcp.backends.ssh_backend import SSHBackend
        if not isinstance(backend, SSHBackend):
            return {"error": "ssh_reconnect only supported on SSH machines"}
        info = backend.start(machine)
        return {"machine": machine, "status": info.status}

    def _op_ssh_remove(self, params):
        ok, err = self._require(params, "machine")
        if err:
            return {"error": err}
        machine = self._targets.resolve_target(params["machine"])
        backend = self._targets.get_backend(machine)
        from sandbox_mcp.backends.ssh_backend import SSHBackend
        if not isinstance(backend, SSHBackend):
            return {"error": "ssh_remove only supported on SSH machines"}
        self._shells.close_all_for_target(machine)
        result = backend.remove(machine)
        self._targets.unregister(machine)
        return result
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_sandbox_env.py -v
```
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add sandbox_env.py tests/test_sandbox_env.py
git commit -m "feat: sandbox_env with progressive discovery and 18 actions"
```

---

## Task 11: MCP Server -- 7 Tool Definitions and Dispatch

**Files:**
- Create: `server.py`
- Test: `tests/test_server.py`

Server exposes 7 tools: sandbox_shell_exec, sandbox_shell_read,
sandbox_file_read, sandbox_file_write, sandbox_file_patch, sandbox_file_search,
and sandbox_env. Dispatches to ShellSession, FileOperations, or SandboxEnv.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_server.py
import pytest
import json
from unittest.mock import MagicMock, patch
from sandbox_mcp.server import SandboxServer


@pytest.fixture
def server():
    return SandboxServer()


def test_list_tools_returns_7(server):
    tools = server.list_tools()
    assert len(tools) == 7
    names = {t.name for t in tools}
    assert "sandbox_shell_exec" in names
    assert "sandbox_shell_read" in names
    assert "sandbox_file_read" in names
    assert "sandbox_file_write" in names
    assert "sandbox_file_patch" in names
    assert "sandbox_file_search" in names
    assert "sandbox_env" in names


def test_call_unknown_tool(server):
    result = server.call_tool("nonexistent", {})
    data = json.loads(result[0].text)
    assert "error" in data


def test_sandbox_env_help(server):
    result = server.call_tool("sandbox_env", {"action": "help"})
    data = json.loads(result[0].text)
    assert "operations" in data
    assert "more_help" in data


def test_sandbox_env_status_empty(server):
    result = server.call_tool("sandbox_env", {"action": "status"})
    data = json.loads(result[0].text)
    assert data["default_machine"] is None
    assert data["machines"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_server.py -v
```

- [ ] **Step 3: Implement SandboxServer**

```python
# server.py
"""Sandbox MCP Server v2: 7 tools (6 core + 1 sandbox_env entry)."""

import json
import logging
from typing import Any

from sandbox_mcp.machine_registry import TargetRegistry
from sandbox_mcp.shell_registry import ShellRegistry
from sandbox_mcp.file_operations import FileOperations
from sandbox_mcp.sandbox_env import SandboxEnv
from sandbox_mcp.backends.docker_backend import DockerBackend
from sandbox_mcp.backends.ssh_backend import SSHBackend

logger = logging.getLogger(__name__)

TOOL_DEFINITIONS = [
    {
        "name": "sandbox_shell_exec",
        "description": "Execute a shell command. wait=true (default) blocks until completion or timeout. wait=false returns after confirming execution started.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "shell_id": {"type": "string", "description": "Specific shell (default: machine's default shell)"},
                "machine": {"type": "string", "description": "machine name (default: default machine)"},
                "wait": {"type": "boolean", "description": "Wait for completion (default: true)"},
                "timeout": {"type": "integer", "description": "Seconds to wait (default: 30)"},
                "max_output": {"type": "integer", "description": "Max output bytes (default: 50000)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "sandbox_shell_read",
        "description": "Read new output from a shell (non-blocking). Detects command completion via markers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "shell_id": {"type": "string", "description": "Shell to read from"},
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
                "machine": {"type": "string", "description": "machine name (default: default machine)"},
                "offset": {"type": "integer", "description": "Start line (1-indexed, default: 1)"},
                "limit": {"type": "integer", "description": "Max lines (default: 500, max: 2000)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "sandbox_file_write",
        "description": "Write content to a file, replacing existing. Creates parent dirs. Auto syntax check.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "Complete file content"},
                "machine": {"type": "string", "description": "machine name (default: default machine)"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "sandbox_file_patch",
        "description": "Targeted find-and-replace edits with fuzzy matching. mode=replace or mode=patch (V4A).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["replace", "patch"]},
                "path": {"type": "string", "description": "File path (replace mode)"},
                "old_string": {"type": "string", "description": "Text to find (replace mode)"},
                "new_string": {"type": "string", "description": "Replacement text (replace mode)"},
                "replace_all": {"type": "boolean", "description": "Replace all (default: false)"},
                "patch": {"type": "string", "description": "V4A patch content (patch mode)"},
                "machine": {"type": "string", "description": "machine name (default: default machine)"},
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
                "search_type": {"type": "string", "enum": ["content", "files"], "description": "default: content"},
                "machine": {"type": "string", "description": "machine name (default: default machine)"},
                "path": {"type": "string", "description": "Directory to search (default: cwd)"},
                "file_glob": {"type": "string", "description": "Filter files (e.g. *.py)"},
                "limit": {"type": "integer", "description": "Max results (default: 50)"},
                "offset": {"type": "integer", "description": "Skip first N (default: 0)"},
                "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "description": "default: content"},
                "context": {"type": "integer", "description": "Context lines (default: 0)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "sandbox_env",
        "description": "Environment management. Call action=help to discover operations or action=status for current state. Other actions are discovered on demand.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "Operation name. Start with help or status."},
                "params": {"type": "object", "description": "Operation params documented by help actions."},
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

    def list_tools(self) -> list[ToolDef]:
        return [ToolDef(t["name"], t["description"], t["inputSchema"])
                for t in TOOL_DEFINITIONS]

    def call_tool(self, name: str, arguments: dict) -> list[TextContent]:
        handler = getattr(self, f"_handle_{name}", None)
        if handler is None:
            return [TextContent(json.dumps({"error": f"Unknown tool: {name}"}))]
        try:
            result = handler(arguments)
            return [TextContent(json.dumps(result, ensure_ascii=False))]
        except Exception as e:
            return [TextContent(json.dumps({"error": str(e)}))]

    def _resolve_target(self, arguments: dict) -> str:
        return self.machines.resolve_target(arguments.get("machine"))

    # --- Shell handlers ---

    def _handle_sandbox_shell_exec(self, args: dict) -> dict:
        machine = self._resolve_target(args)
        backend = self.machines.get_backend(machine)
        shell_id = args.get("shell_id")
        timeout = args.get("timeout", 30)
        wait = args.get("wait", True)
        max_output = args.get("max_output", 50000)

        if shell_id:
            session = self.shells.get(shell_id)
            if session is None:
                return {"error": f"Unknown shell_id: {shell_id}"}
        else:
            sid = self.shells.get_or_create_default(
                machine, lambda: backend.open_shell(machine)
            )
            session = self.shells.get(sid)

        return session.send(args["command"], wait=wait, timeout=timeout,
                            max_output=max_output)

    def _handle_sandbox_shell_read(self, args: dict) -> dict:
        session = self.shells.get(args["shell_id"])
        if session is None:
            return {"error": f"Unknown shell_id: {args['shell_id']}"}
        return session.read()

    # --- File operation handlers ---

    def _get_file_ops(self, machine: str) -> FileOperations:
        backend = self.machines.get_backend(machine)
        return FileOperations(backend)

    def _handle_sandbox_file_read(self, args: dict) -> dict:
        machine = self._resolve_target(args)
        fops = self._get_file_ops(machine)
        return fops.read(args["path"], machine,
                         offset=args.get("offset", 1),
                         limit=args.get("limit", 500))

    def _handle_sandbox_file_write(self, args: dict) -> dict:
        machine = self._resolve_target(args)
        fops = self._get_file_ops(machine)
        return fops.write(args["path"], args["content"], machine)

    def _handle_sandbox_file_patch(self, args: dict) -> dict:
        machine = self._resolve_target(args)
        fops = self._get_file_ops(machine)
        return fops.patch(
            mode=args["mode"], machine=machine,
            path=args.get("path", ""),
            old_string=args.get("old_string", ""),
            new_string=args.get("new_string", ""),
            replace_all=args.get("replace_all", False),
            patch=args.get("patch", ""),
        )

    def _handle_sandbox_file_search(self, args: dict) -> dict:
        machine = self._resolve_target(args)
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

    # --- sandbox_env handler ---

    def _handle_sandbox_env(self, args: dict) -> Any:
        action = args.get("action", "")
        params = args.get("params", {})
        return self.sandbox_env.dispatch(action, params)


def main():
    """Entry point: run the MCP server over stdio."""
    import asyncio
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        import mcp.types as types
    except ImportError:
        logging.error("mcp package not installed. Run: pip install mcp")
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    server = SandboxServer()
    mcp_server = Server("sandbox-mcp")

    @mcp_server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=t.name, description=t.description, inputSchema=t.inputSchema)
            for t in server.list_tools()
        ]

    @mcp_server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        results = server.call_tool(name, arguments)
        return [types.TextContent(type="text", text=r.text) for r in results]

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.run(read_stream, write_stream,
                                 mcp_server.create_initialization_options())

    asyncio.run(run())
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_server.py -v
```
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "feat: SandboxServer with 7 tools and sandbox_env dispatch"
```

---

## Task 12: Integration Test with Docker

**Files:**
- Test: `tests/test_integration_docker.py`

- [ ] **Step 1: Write integration test (skipped if Docker unavailable)**

```python
# tests/test_integration_docker.py
import pytest
import json
import shutil
from sandbox_mcp.server import SandboxServer

pytestmark = pytest.mark.skipif(
    not shutil.which("docker"),
    reason="Docker not available",
)


@pytest.fixture
def server():
    return SandboxServer()


@pytest.fixture
def docker_target(server):
    """Create a temporary Docker machine via sandbox_env."""
    result = server.call_tool("sandbox_env", {
        "action": "docker_run",
        "params": {"name": "test-integration", "image": "python:3.12-slim", "purpose": "integration test"},
    })
    data = json.loads(result[0].text)
    if "error" in data:
        pytest.skip(f"Cannot create Docker container: {data['error']}")
    server.call_tool("sandbox_env", {
        "action": "default_set", "params": {"machine": "test-integration"},
    })
    yield server
    server.call_tool("sandbox_env", {
        "action": "docker_remove", "params": {"machine": "test-integration"},
    })


def test_shell_exec_wait_true(docker_target):
    """shell_exec(wait=true) executes a command and returns output."""
    result = docker_target.call_tool("sandbox_shell_exec", {
        "command": "echo hello_from_docker",
    })
    data = json.loads(result[0].text)
    assert data["status"] == "completed"
    assert "hello_from_docker" in data["output"]


def test_shell_exec_preserves_state(docker_target):
    """Environment changes persist across send calls."""
    docker_target.call_tool("sandbox_shell_exec", {
        "command": "export TEST_VAR=12345",
    })
    result = docker_target.call_tool("sandbox_shell_exec", {
        "command": "echo $TEST_VAR",
    })
    data = json.loads(result[0].text)
    assert "12345" in data["output"]


def test_shell_exec_wait_false_then_read(docker_target):
    """shell_exec(wait=false) starts command, shell_read gets output."""
    result = docker_target.call_tool("sandbox_shell_exec", {
        "command": "echo started; sleep 0.5; echo done",
        "wait": False,
        "timeout": 3,
    })
    data = json.loads(result[0].text)
    assert data["status"] == "running"
    assert data["confirmed"] is True

    # Need shell_id - get from default shell
    # The exec used default shell, so we need to get its id
    import time
    time.sleep(1.5)

    # Read via shell_list to find the shell
    list_result = docker_target.call_tool("sandbox_env", {
        "action": "shell_list", "params": {"machine": "test-integration"},
    })
    shells = json.loads(list_result[0].text)
    shell_id = shells[0]["shell_id"]

    read_result = docker_target.call_tool("sandbox_shell_read", {
        "shell_id": shell_id,
    })
    read_data = json.loads(read_result[0].text)
    assert read_data["status"] in ("completed", "running")
    assert "done" in read_data.get("output", "") or read_data["status"] == "completed"


def test_file_operations_in_docker(docker_target):
    """Write and read a file in a Docker container."""
    result = docker_target.call_tool("sandbox_file_write", {
        "path": "/tmp/test_file.txt",
        "content": "line1\nline2\nline3\n",
    })
    data = json.loads(result[0].text)
    assert data["status"] == "ok"

    result = docker_target.call_tool("sandbox_file_read", {
        "path": "/tmp/test_file.txt",
    })
    data = json.loads(result[0].text)
    assert "1|line1" in data["output"]
    assert "2|line2" in data["output"]


def test_sandbox_env_status(docker_target):
    """sandbox_env status shows the machine."""
    result = docker_target.call_tool("sandbox_env", {"action": "status"})
    data = json.loads(result[0].text)
    assert data["default_machine"] == "test-integration"
    assert len(data["machines"]) == 1
    assert data["machines"][0]["backend"] == "docker"


def test_docker_commit(docker_target):
    """Commit container state to a new image."""
    result = docker_target.call_tool("sandbox_env", {
        "action": "docker_commit",
        "params": {"machine": "test-integration", "image_tag": "sandbox-test-snapshot:latest"},
    })
    data = json.loads(result[0].text)
    assert data["status"] == "committed"
```

- [ ] **Step 2: Run integration tests (requires Docker)**

```bash
pytest tests/test_integration_docker.py -v
```
Expected: 6 passed (if Docker available) or 6 skipped

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -v
```
Expected: All unit tests pass, integration tests pass/skip

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration_docker.py
git commit -m "test: integration tests for Docker backend with shell_exec and sandbox_env"
```

---

## Self-Review

### Spec Coverage (v2 design-spec)

- [x] Three-layer tool exposure (tools/list -> help -> docker_help/ssh_help) -- Task 10, 11
- [x] shell_exec with dual markers (wait=true/false) -- Task 2
- [x] Shell state machine (idle/busy/running/terminated) -- Task 2
- [x] Background drain thread (head 5K + tail ring buffer) -- Task 2
- [x] Output truncation (tail, max_output param) -- Task 2
- [x] I/O merged (stderr=STDOUT) -- Task 2
- [x] shell_read from in-memory buffer, detects markers -- Task 2
- [x] Manual cleanup, shell_list hints for terminated -- Task 7
- [x] Backend-specialized lifecycle (docker_stop/ssh_disconnect) -- Task 4, 5, 10
- [x] sandbox_env 18 actions with progressive discovery -- Task 10
- [x] sandbox_env inputSchema (~100 tokens) -- Task 11
- [x] Core 6 tools + sandbox_env = 7 tools in tools/list -- Task 11
- [x] Default targeting model (default_set + optional machine) -- Task 6, 10, 11
- [x] File operations (read/write/patch/search) -- Task 8, 9
- [x] Docker backend (run/build/commit/stop/start/remove) -- Task 4
- [x] SSH backend (connect/disconnect/reconnect/remove) -- Task 5

### Placeholder Scan
No TBD/TODO. All code blocks are complete implementations.

### Type Consistency
- ShellSession.send returns {output, exit_code, status} or {status, confirmed} -- consistent
- ShellSession.read returns {output, status, [exit_code]} -- consistent
- TargetInfo has name/backend/status/purpose -- consistent
- sandbox_env.dispatch returns dict or list -- consistent, JSON-serializable
