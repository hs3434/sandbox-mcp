"""Docker backend: manages containers via the docker CLI."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from pathlib import Path

from sandbox_mcp.backends.base import Backend, TargetInfo
from sandbox_mcp.shell_session import ShellSession


def _run(cmd, timeout=30):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _TimeoutResult()


class _TimeoutResult:
    """Stand-in for CompletedProcess when subprocess.run times out."""
    returncode = -1
    stdout = ""
    stderr = "timeout"


class DockerBackend(Backend):
    """Docker backend: run, stop, start, remove, commit, build, open_shell, exec_oneoff."""

    def __init__(self):
        self._docker_path: str | None = None
        self._started_at: dict[str, float] = {}

    def _docker(self) -> str:
        if self._docker_path is None:
            p = shutil.which("docker")
            if not p:
                raise RuntimeError("docker not found on PATH")
            self._docker_path = p
        return self._docker_path

    # ---- lifecycle ----

    def create(self, name: str, purpose: str = "", **kwargs) -> TargetInfo:
        image = kwargs.get("image", "python:3.12-slim")
        volumes = kwargs.get("volumes", []) or []
        ports = kwargs.get("ports", []) or []
        env = kwargs.get("env", {}) or {}
        workdir = kwargs.get("workdir", "/workspace")

        container = f"sandbox-{name}"
        cmd = [self._docker(), "run", "-d", "--name", container,
               "--init", "--restart", "on-failure:3",
               "-w", workdir]
        for v in volumes:
            cmd += ["-v", v]
        for p in ports:
            cmd += ["-p", p]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [image, "sleep", "infinity"]

        result = _run(cmd, timeout=60)
        if result.returncode != 0:
            return TargetInfo(name=name, backend="docker", status="error",
                              purpose=purpose)
        self._started_at[name] = time.time()
        return TargetInfo(name=name, backend="docker", status="running",
                          purpose=purpose)

    def start(self, name: str) -> TargetInfo:
        container = f"sandbox-{name}"
        result = _run([self._docker(), "start", container], timeout=30)
        if result.returncode != 0:
            return TargetInfo(name=name, backend="docker", status="error",
                              error=result.stderr.strip() or "docker start failed")
        self._started_at[name] = self._started_at.get(name, time.time())
        return TargetInfo(name=name, backend="docker", status="running")

    def stop(self, name: str) -> TargetInfo:
        container = f"sandbox-{name}"
        result = _run([self._docker(), "stop", container], timeout=30)
        if result.returncode != 0:
            return TargetInfo(name=name, backend="docker", status="error",
                              error=result.stderr.strip() or "docker stop failed")
        return TargetInfo(name=name, backend="docker", status="stopped")

    def remove(self, name: str) -> dict:
        container = f"sandbox-{name}"
        result = _run([self._docker(), "rm", "-f", container], timeout=30)
        if result.returncode != 0:
            return {"target": name, "status": "error",
                    "error": result.stderr.strip() or "docker rm failed"}
        self._started_at.pop(name, None)
        return {"target": name, "status": "removed"}

    def get_info(self, name: str) -> TargetInfo:
        container = f"sandbox-{name}"
        result = _run([self._docker(), "inspect", "--format",
                       "{{.State.Running}}", container], timeout=10)
        if result.returncode != 0:
            return TargetInfo(name=name, backend="docker", status="error")
        running = result.stdout.strip() == "true"
        return TargetInfo(
            name=name, backend="docker",
            status="running" if running else "stopped",
        )

    # ---- docker-specific extras ----

    def commit(self, name: str, image_tag: str | None = None) -> dict:
        container = f"sandbox-{name}"
        tag = image_tag or f"sandbox-{name}-{int(time.time())}"
        result = _run([self._docker(), "commit", container, tag], timeout=120)
        if result.returncode != 0:
            return {"error": result.stderr.strip() or "commit failed"}
        return {"image_tag": tag, "status": "committed"}

    def build(self, image_tag: str, dockerfile: str,
              context_dir: str | None = None) -> dict:
        ctx = context_dir or "."
        df_path = Path(dockerfile)
        if not df_path.is_absolute():
            df_path = Path(ctx) / df_path
        if not df_path.exists():
            return {"error": f"Dockerfile not found: {df_path}"}
        result = _run([self._docker(), "build", "-t", image_tag,
                       "-f", str(df_path), ctx], timeout=600)
        if result.returncode != 0:
            return {"error": result.stderr.strip() or "build failed",
                    "image_tag": image_tag, "status": "error"}
        return {"image_tag": image_tag, "status": "built"}

    # ---- shell / exec ----

    def open_shell(self, name: str) -> ShellSession:
        container = f"sandbox-{name}"
        return ShellSession([self._docker(), "exec", "-i", container, "bash"])

    def exec_oneoff(self, name: str, command: str, timeout: int = 30) -> dict:
        container = f"sandbox-{name}"
        result = _run(
            [self._docker(), "exec", container, "bash", "-c", command],
            timeout=timeout,
        )
        return {
            "exit_code": result.returncode,
            "output": result.stdout or "",
            "stderr": result.stderr or "",
        }

    def suggest_paths(self, name: str, missing_path: str) -> list:
        """Suggest similar paths in the container using ls/find."""
        container = f"sandbox-{name}"
        dirname = str(Path(missing_path).parent)
        basename = Path(missing_path).name
        ls_cmd = (
            f"ls -1 {shlex.quote(dirname)} 2>/dev/null | "
            f"grep -i {shlex.quote(basename)} | head -5"
        )
        result = _run(
            [self._docker(), "exec", container, "bash", "-c", ls_cmd],
            timeout=10,
        )
        if result.returncode != 0:
            return []
        prefix = dirname.rstrip("/")
        return [
            f"{prefix}/{line.strip()}"
            for line in result.stdout.splitlines()
            if line.strip()
        ]
