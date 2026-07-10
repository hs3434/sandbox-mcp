"""ShellSession: persistent bash process with dual-marker I/O and drain thread.

States: idle | busy | running | terminated
  idle       - no command running, bash at prompt
  busy       - send(wait=true) blocking, lock held
  running    - command executing in background (wait=false or timeout)
  terminated - bash process exited (passive close)
"""

from __future__ import annotations

import os
import re
import select
import subprocess
import threading
import time
import uuid
from collections import deque

_MARKER_RE = re.compile(r"__(START|END)_[0-9a-f]+__(?::\d+)?")


class ShellSession:
    """A persistent shell (bash) process with drain-thread-based I/O."""

    HEAD_SIZE = 5120        # 5KB head buffer
    TAIL_SIZE = 46080       # ~45KB tail ring buffer
    DEFAULT_MAX_OUTPUT = 50000  # 50KB default output limit

    def __init__(self, args):
        self._args = args
        self._process = None
        self._lock = threading.Lock()
        self._state = "idle"
        self._last_command = None
        self._started_at = time.time()
        self._purpose = None

        # Drain thread buffer
        self._head = bytearray()
        self._tail = deque(maxlen=self.TAIL_SIZE)
        self._head_done = False
        self._total_bytes = 0

        # Marker tracking
        self._pending_start_marker = None
        self._pending_end_marker = None
        self._pending_exit_code = None
        self._start_event = threading.Event()
        self._end_event = threading.Event()

        self._drain_thread = None
        self._start()

    def _start(self):
        self._process = subprocess.Popen(
            self._args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self._state = "idle"
        self._drain_thread = threading.Thread(target=self._drain, daemon=True)
        self._drain_thread.start()

    def _drain(self):
        """Background thread: read stdout, buffer data, detect markers.

        Marker detection is per-chunk: each read from the pipe is searched
        directly for the pending markers. No accumulator is needed because
        bash writes each marker on its own line, so a chunk from `os.read`
        will contain the full marker line.
        """
        proc = self._process
        while True:
            try:
                ready, _, _ = select.select([proc.stdout], [], [], 0.1)
            except (ValueError, OSError):
                break
            if ready:
                try:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                except (ValueError, OSError):
                    break
                if not chunk:
                    break
                self._total_bytes += len(chunk)

                if not self._head_done:
                    remaining = self.HEAD_SIZE - len(self._head)
                    if remaining > 0:
                        take = chunk[:remaining]
                        self._head.extend(take)
                        leftover = chunk[remaining:]
                        if leftover:
                            self._tail.extend(leftover)
                            self._head_done = True
                    else:
                        self._tail.extend(chunk)
                        self._head_done = True
                else:
                    self._tail.extend(chunk)

                text = chunk.decode("utf-8", errors="replace")

                if self._pending_start_marker and not self._start_event.is_set():
                    if self._pending_start_marker in text:
                        self._start_event.set()

                if self._pending_end_marker and not self._end_event.is_set():
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
            else:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)

        self._state = "terminated"
        self._start_event.set()
        self._end_event.set()

    def send(self, command, wait=True, timeout=30, max_output=DEFAULT_MAX_OUTPUT):
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

            self._pending_start_marker = start_marker
            self._pending_end_marker = end_marker
            self._pending_exit_code = None
            self._start_event.clear()
            self._end_event.clear()

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
            except (BrokenPipeError, OSError):
                self._state = "terminated"
                return {"output": "", "exit_code": None, "status": "terminated"}

        if wait:
            if self._end_event.wait(timeout=timeout):
                exit_code = self._pending_exit_code
                output = self._get_buffered_output(max_output)
                with self._lock:
                    if self._state != "terminated":
                        self._state = "idle"
                return {"output": output, "exit_code": exit_code, "status": "completed"}
            output = self._get_buffered_output(max_output)
            with self._lock:
                if self._state == "busy":
                    self._state = "running"
            return {"output": output, "exit_code": None, "status": "running"}

        if self._start_event.wait(timeout=2.0):
            with self._lock:
                if self._state == "terminated":
                    return {"status": "terminated", "confirmed": False}
            return {"status": "running", "confirmed": True}
        with self._lock:
            if self._state == "terminated":
                return {"status": "terminated", "confirmed": False}
        return {"status": "running", "confirmed": False}

    def read(self):
        """Non-blocking read of new output from the buffer."""
        with self._lock:
            if self._state == "terminated":
                output = self._get_buffered_output(self.DEFAULT_MAX_OUTPUT)
                return {"output": output, "status": "terminated"}

            if self._state == "idle":
                return {"output": "", "status": "idle"}

            if self._end_event.is_set() and self._pending_exit_code is not None:
                output = self._get_buffered_output(self.DEFAULT_MAX_OUTPUT)
                self._state = "idle"
                return {"output": output, "exit_code": self._pending_exit_code,
                        "status": "completed"}

            output = self._get_buffered_output(self.DEFAULT_MAX_OUTPUT)
            return {"output": output, "status": "running"}

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
            try:
                self._process.kill()
                self._process.wait(timeout=5)
            except Exception:
                pass
            self._process = None
        self._start_event.set()
        self._end_event.set()

    @property
    def state(self):
        return self._state

    @property
    def last_command(self):
        return self._last_command

    @property
    def uptime(self):
        return time.time() - self._started_at

    @property
    def purpose(self):
        return self._purpose

    @purpose.setter
    def purpose(self, value):
        self._purpose = value
