"""SSH backend: manages remote machines via SSH with ControlMaster (key auth only)."""

from __future__ import annotations

import shutil
import subprocess
import time

from backends.base import Backend, TargetInfo
from shell_session import ShellSession


def _find_ssh():
    p = shutil.which("ssh")
    if not p:
        raise RuntimeError("ssh not found on PATH")
    return p


class SSHBackend(Backend):
    """SSH remote machine backend with ControlMaster multiplexing."""

    def __init__(self):
        self._ssh = _find_ssh()
        self._targets: dict[str, dict] = {}

    def _socket_path(self, name):
        return f"/tmp/sandbox-mcp-ssh-{name}"

    def _ssh_base_args(self, name):
        target = self._targets.get(name, {})
        socket = self._socket_path(name)
        args = [self._ssh, "-o", f"ControlPath={socket}",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10"]
        port = target.get("port", 22)
        args += ["-p", str(port)]
        key = target.get("key")
        if key:
            args += ["-i", key]
        user = target.get("user", "")
        host = target.get("host", "")
        args.append(f"{user}@{host}" if user else host)
        return args

    def create(self, name, purpose="", **kwargs):
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
            cmd += ["-i", key]
        cmd.append(f"{user}@{host}")
        cmd.append("true")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return TargetInfo(name=name, backend="ssh", status="error",
                              purpose=purpose)
        if result.returncode != 0:
            return TargetInfo(name=name, backend="ssh", status="error",
                              purpose=purpose)
        return TargetInfo(name=name, backend="ssh", status="running",
                          purpose=purpose)

    def start(self, name):
        """Reconnect SSH ControlMaster."""
        target = self._targets.get(name, {})
        return self.create(name, **{k: v for k, v in target.items()
                                     if k in ("host", "user", "port", "key")})

    def stop(self, name):
        """Close the SSH master connection."""
        socket = self._socket_path(name)
        target = self._targets.get(name, {})
        user = target.get("user", "")
        host = target.get("host", "")
        try:
            subprocess.run(
                [self._ssh, "-S", socket, "-O", "exit", f"{user}@{host}"],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
        return TargetInfo(name=name, backend="ssh", status="stopped")

    def remove(self, name):
        if name in self._targets:
            try:
                self.stop(name)
            except Exception:
                pass
            self._targets.pop(name, None)
        return {"target": name, "status": "removed"}

    def get_info(self, name):
        target = self._targets.get(name)
        if not target:
            return TargetInfo(name=name, backend="ssh", status="error")
        try:
            result = subprocess.run(
                self._ssh_base_args(name) + ["true"],
                capture_output=True, text=True, timeout=10,
            )
            status = "running" if result.returncode == 0 else "error"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            status = "error"
        return TargetInfo(name=name, backend="ssh", status=status,
                          purpose=target.get("purpose", ""))

    def open_shell(self, name):
        return ShellSession(self._ssh_base_args(name) + ["bash"])

    def exec_oneoff(self, name, command, timeout=30):
        try:
            result = subprocess.run(
                self._ssh_base_args(name) + ["bash", "-c", command],
                capture_output=True, text=True, timeout=timeout,
            )
            return {"exit_code": result.returncode,
                    "output": result.stdout or "",
                    "stderr": result.stderr or ""}
        except subprocess.TimeoutExpired:
            return {"exit_code": None, "output": "", "stderr": "timeout"}
