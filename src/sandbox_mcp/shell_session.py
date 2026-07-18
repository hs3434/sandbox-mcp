# sandbox-mcp - Sandbox Environment Manager MCP server
# Copyright (C) 2024  Sandbox MCP Contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""ShellSession: persistent bash process with dual-marker I/O and drain thread.

States: idle | busy | running | terminated
  idle       - no command running, bash at prompt
  busy       - send(wait=true) blocking, lock held
  running    - command executing in background (wait=false or timeout)
  terminated - bash process exited (passive close)

Buffer sizes and the default output cap are configurable via
``[shell]`` in ``~/.sandbox-mcp/config.toml`` (or the
``SANDBOX_MCP_SHELL_*`` env vars).
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import signal
import subprocess
import threading
import time
import uuid
from collections import deque

from sandbox_mcp.config import load as _load_config

_MARKER_RE = re.compile(r"__(START|END)_[0-9a-f]+__(?::\d+)?")


class ShellSession:
    """A persistent shell (bash) process with drain-thread-based I/O."""

    def __init__(self, args=None, process=None):
        """Create a shell session.

        Either *args* (a ``subprocess.Popen`` argument list) or *process*
        (an object with ``.stdin``, ``.stdout``, ``.poll``, ``.kill``,
        ``.wait`` methods matching ``subprocess.Popen``) must be provided.
        The *process* form is used by backends that provide their own
        process-like handle (e.g. the Docker backend's SDK-based exec).
        """
        shell_cfg = _load_config().shell
        self.HEAD_SIZE = shell_cfg.head_size
        self.TAIL_SIZE = shell_cfg.tail_size
        self.DEFAULT_MAX_OUTPUT = shell_cfg.default_max_output
        self._args = args
        self._process = process
        self._external = process is not None
        self._lock = threading.Lock()
        self._state = "idle"
        self._last_command = None
        self._started_at = time.time()

        # Drain thread buffer
        self._head = bytearray()
        self._tail = deque(maxlen=self.TAIL_SIZE)
        self._head_done = False

        # Marker tracking
        self._pending_start_marker = None
        self._pending_end_marker = None
        self._pending_exit_code = None
        self._start_event = threading.Event()
        self._end_event = threading.Event()

        self._drain_thread = None
        self._start()

    def _start(self):
        if self._external:
            # External process was already started by the caller.
            self._state = "idle"
            self._drain_thread = threading.Thread(target=self._drain, daemon=True)
            self._drain_thread.start()
            return
        self._process = subprocess.Popen(
            self._args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            # New process group + session so close() can killpg() the
            # whole tree.  Without this, a long-running child like
            # ``sleep 60`` inherits bash's stdout pipe FD and keeps it
            # open after bash is killed — the drain thread blocks on
            # readline waiting for EOF that never comes, and close()
            # hits its drain_thread.join(timeout=2) every time.
            start_new_session=True,
        )
        self._state = "idle"
        self._drain_thread = threading.Thread(target=self._drain, daemon=True)
        self._drain_thread.start()

    def _drain(self):
        """Background thread: read stdout line-by-line, detect markers.

        bash emits `__START_<uuid>__` and `__END_<uuid>__:$?` on their own
        lines (terminated by `\\n`), so `readline()` always returns a
        complete marker. The user-visible output buffer (`_head` + `_tail`)
        is filled from the same lines.
        """
        proc = self._process
        stdout = proc.stdout  # BufferedReader; readline() returns bytes

        while True:
            try:
                ready, _, _ = select.select([stdout], [], [], 0.1)
            except (ValueError, OSError):
                break
            if not ready:
                continue
            try:
                line = stdout.readline()
            except (ValueError, OSError):
                break
            if not line:
                # EOF: bash closed its stdout.
                break
            self._store_output(line)

            start_tag = (
                self._pending_start_marker.encode("utf-8") if self._pending_start_marker else None
            )
            end_tag = self._pending_end_marker.encode("utf-8") if self._pending_end_marker else None
            if start_tag is not None and not self._start_event.is_set() and start_tag in line:
                self._start_event.set()

            if end_tag is not None and not self._end_event.is_set():
                end_prefix = end_tag + b":"
                if end_prefix in line:
                    after = line[line.index(end_prefix) + len(end_prefix) :]
                    code_str = after.strip()
                    try:
                        self._pending_exit_code = int(code_str)
                    except ValueError:
                        self._pending_exit_code = 0
                    self._end_event.set()

        self._state = "terminated"
        self._start_event.set()
        self._end_event.set()

    def _store_output(self, data):
        """Feed one bytes line into the head/tail ring buffer."""
        if not self._head_done:
            remaining = self.HEAD_SIZE - len(self._head)
            if remaining > 0:
                take = data[:remaining]
                self._head.extend(take)
                leftover = data[remaining:]
                if leftover:
                    self._tail.extend(leftover)
                    self._head_done = True
            else:
                self._tail.extend(data)
                self._head_done = True
        else:
            self._tail.extend(data)

    def _with_pid(self, result: dict) -> dict:
        """Tag a result dict with the current bash process id.

        Agents track this across calls; a change means the shell was
        restarted and in-memory state (exports, cwd, jobs) is gone.
        """
        pid = self.bash_pid
        if pid is not None:
            result["bash_pid"] = pid
        return result

    def send(self, command, wait=True, timeout=30, max_output=None):
        """Send a command to the shell.

        wait=True:  block until __END_ marker or timeout
        wait=False: block until __START_ marker (~2s), then return

        ``max_output`` defaults to the configured per-session cap
        (``[shell] default_max_output``); pass an explicit value to
        override for one call.

        Every result dict includes ``bash_pid`` so callers can detect
        when the underlying shell has been restarted.
        """
        if max_output is None:
            max_output = self.DEFAULT_MAX_OUTPUT
        with self._lock:
            if self._state in ("terminated", "closed"):
                return self._with_pid(
                    {
                        "output": "",
                        "exit_code": None,
                        "status": "error",
                        "error": "Shell is terminated",
                    }
                )
            if self._state in ("busy", "running"):
                return self._with_pid(
                    {
                        "output": "",
                        "exit_code": None,
                        "status": "error",
                        "error": "Shell is busy (previous command still running). "
                        "Use shell_read to check or shell_remove to kill.",
                    }
                )

            marker = uuid.uuid4().hex
            start_marker = f"__START_{marker}__"
            end_marker = f"__END_{marker}__"
            full_input = f"echo {start_marker}\n{command}\necho {end_marker}:$?\n"

            self._pending_start_marker = start_marker
            self._pending_end_marker = end_marker
            self._pending_exit_code = None
            self._start_event.clear()
            self._end_event.clear()

            self._head = bytearray()
            self._tail = deque(maxlen=self.TAIL_SIZE)
            self._head_done = False
            self._last_command = command

            if wait:
                self._state = "busy"
            else:
                self._state = "running"

            try:
                self._process.stdin.write(full_input.encode())
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._state = "terminated"
                return self._with_pid({"output": "", "exit_code": None, "status": "terminated"})

        if wait:
            if self._end_event.wait(timeout=timeout):
                exit_code = self._pending_exit_code
                output = self._get_buffered_output(max_output)
                with self._lock:
                    if self._state != "terminated":
                        self._state = "idle"
                return self._with_pid(
                    {"output": output, "exit_code": exit_code, "status": "completed"}
                )
            output = self._get_buffered_output(max_output)
            with self._lock:
                if self._state == "busy":
                    self._state = "running"
            return self._with_pid({"output": output, "exit_code": None, "status": "running"})

        if self._start_event.wait(timeout=2.0):
            with self._lock:
                if self._state == "terminated":
                    return self._with_pid({"status": "terminated", "confirmed": False})
            return self._with_pid({"status": "running", "confirmed": True})
        with self._lock:
            if self._state == "terminated":
                return self._with_pid({"status": "terminated", "confirmed": False})
        return self._with_pid({"status": "running", "confirmed": False})

    def read(self):
        """Non-blocking read of new output from the buffer."""
        with self._lock:
            if self._state == "terminated":
                output = self._get_buffered_output(self.DEFAULT_MAX_OUTPUT)
                return self._with_pid({"output": output, "status": "terminated"})

            if self._state == "idle":
                return self._with_pid({"output": "", "status": "idle"})

            if self._end_event.is_set() and self._pending_exit_code is not None:
                output = self._get_buffered_output(self.DEFAULT_MAX_OUTPUT)
                self._state = "idle"
                return self._with_pid(
                    {
                        "output": output,
                        "exit_code": self._pending_exit_code,
                        "status": "completed",
                    }
                )

            output = self._get_buffered_output(self.DEFAULT_MAX_OUTPUT)
            return self._with_pid({"output": output, "status": "running"})

    def _get_buffered_output(self, max_output):
        """Get buffered output, truncating if necessary."""
        head_text = self._head.decode("utf-8", errors="replace")
        tail_text = bytes(self._tail).decode("utf-8", errors="replace")

        head_text = _MARKER_RE.sub("", head_text)
        tail_text = _MARKER_RE.sub("", tail_text)

        full = head_text + tail_text

        if len(full) <= max_output:
            return full.strip("\n")

        truncated = full[-max_output:]
        notice = f"\n[Output truncated: showing last {max_output} of {len(full)} chars]\n"
        return (notice + truncated).strip("\n")

    def write_stdin(self, data):
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

    def close(self):
        """Kill the shell process and stop drain thread."""
        with self._lock:
            self._state = "terminated"
        if self._process:
            # Kill the whole process group (bash + any descendants like
            # ``sleep 60``) so the stdout pipe closes immediately.  Falls
            # back to direct kill for externally-provided processes
            # (e.g. Docker exec fds) that don't own a process group.
            try:
                if hasattr(self._process, "pid") and self._process.pid is not None:
                    pgid = os.getpgid(self._process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    self._process.kill()
                self._process.wait(timeout=5)
            except (ProcessLookupError, PermissionError):
                # Already gone or not our group — try plain kill.
                with contextlib.suppress(Exception):
                    self._process.kill()
                with contextlib.suppress(Exception):
                    self._process.wait(timeout=5)
            except Exception:
                pass
            self._process = None
        self._start_event.set()
        self._end_event.set()
        if self._drain_thread:
            self._drain_thread.join(timeout=2)
            self._drain_thread = None

    @property
    def state(self):
        return self._state

    @property
    def bash_pid(self):
        """Underlying bash process identifier (or None for external procs).

        Local ``bash`` Popen: real OS PID (int).
        External ``DockerExecProcess`` / SSH: backend-specific ID (str) —
        for Docker it's the exec instance ID returned by the daemon.
        Changes between calls mean the shell was restarted, so any
        in-memory state (exports, cwd, jobs) is gone.
        """
        proc = self._process
        if proc is None:
            return None
        # DockerExecProcess exposes exec_id publicly; Popen exposes pid.
        return getattr(proc, "exec_id", None) or getattr(proc, "pid", None)

    @property
    def last_command(self):
        return self._last_command

    @property
    def uptime(self):
        return time.time() - self._started_at
