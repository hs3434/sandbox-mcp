"""Docker backend: manages containers via the docker Python SDK.

Lifecycle operations (create, stop, start, remove, commit, build) use
the SDK's structured API: no shell-command construction, no stderr
parsing, no ARG_MAX limits.

Shell operations (open_shell, exec_oneoff) still use``docker exec``
via subprocess because ShellSession tracks a long-running Popen for
persistent bash.  Using the SDK's ``exec_run(…, socket=True)`` for the
shell session would require a different transport layer and is not
necessary for the current architecture.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound, NotFound

from sandbox_mcp.backends.base import Backend, TargetInfo
from sandbox_mcp.shell_session import ShellSession


class DockerBackend(Backend):
    """Docker container backend powered by ``docker.DockerClient``."""

    def __init__(self):
        self._client: docker.DockerClient | None = None
        self._started_at: dict[str, float] = {}

    def _ensure_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _container_name(self, name: str) -> str:
        return f"sandbox-{name}"

    # ---- lifecycle ----

    def create(self, name: str, purpose: str = "", **kwargs) -> TargetInfo:
        image = kwargs.get("image", "python:3.12-slim")
        volumes = kwargs.get("volumes", []) or []
        ports = kwargs.get("ports", []) or []
        env = kwargs.get("env", {}) or {}
        workdir = kwargs.get("workdir", "/workspace")

        container_name = self._container_name(name)

        # Build port mappings in docker-py format.
        port_bindings: dict = {}
        for p in ports:
            # Simple format: host:container or just container
            host_part, _, container_part = p.partition(":")
            if container_part:
                cp = container_part.split("/")[0]
                binding = {"HostIp": "0.0.0.0", "HostPort": host_part}
                try:
                    port_bindings[int(cp)] = [binding]
                except ValueError:
                    port_bindings[cp] = [binding]
            else:
                cp = p.split("/")[0]
                try:
                    port_bindings[int(cp)] = None
                except ValueError:
                    port_bindings[cp] = None

        # Volume dict format: {host_path: {"bind": container_path, "mode": "rw"}}
        volume_bindings = {}
        for v in volumes:
            if ":" in v:
                host_v, container_v = v.split(":", 1)
                volume_bindings[host_v] = {"bind": container_v, "mode": "rw"}

        try:
            self._ensure_client().containers.run(
                image,
                detach=True,
                name=container_name,
                init=True,
                restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
                working_dir=workdir,
                volumes=volume_bindings if volume_bindings else None,
                ports=port_bindings if port_bindings else None,
                environment=env or None,
                command="sleep infinity",
            )
        except APIError as e:
            return TargetInfo(name=name, backend="docker", status="error",
                              purpose=purpose,
                              error=str(e.explanation or e))
        except ImageNotFound:
            return TargetInfo(name=name, backend="docker", status="error",
                              purpose=purpose,
                              error=f"Image {image} not found")

        self._started_at[name] = time.time()
        return TargetInfo(name=name, backend="docker", status="running",
                          purpose=purpose)

    def start(self, name: str) -> TargetInfo:
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
            container.start()
            self._started_at[name] = self._started_at.get(name, time.time())
            return TargetInfo(name=name, backend="docker", status="running")
        except (APIError, NotFound) as e:
            return TargetInfo(name=name, backend="docker", status="error",
                              error=str(e))

    def stop(self, name: str) -> TargetInfo:
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
            container.stop(timeout=10)
            return TargetInfo(name=name, backend="docker", status="stopped")
        except (APIError, NotFound) as e:
            return TargetInfo(name=name, backend="docker", status="error",
                              error=str(e))

    def remove(self, name: str) -> dict:
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
            container.remove(force=True)
            self._started_at.pop(name, None)
            return {"target": name, "status": "removed"}
        except (APIError, NotFound) as e:
            return {"target": name, "status": "error", "error": str(e)}

    def get_info(self, name: str) -> TargetInfo:
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
            status = container.attrs.get("State", {}).get("Status", "unknown")
            running = status == "running"
            return TargetInfo(
                name=name, backend="docker",
                status="running" if running else "stopped",
            )
        except (APIError, NotFound):
            return TargetInfo(name=name, backend="docker", status="error")

    # ---- docker-specific extras ----

    def commit(self, name: str, image_tag: str | None = None) -> dict:
        container_name = self._container_name(name)
        tag = image_tag or f"sandbox-{name}-{int(time.time())}"
        try:
            container = self._ensure_client().containers.get(container_name)
            repo, tag_part = (tag.rsplit(":", 1) + [""])[:2]
            container.commit(repository=repo or "sandbox-mcp",
                             tag=tag_part or "latest")
            return {"image_tag": tag, "status": "committed"}
        except (APIError, NotFound) as e:
            return {"error": str(e), "image_tag": tag, "status": "error"}

    def build(self, image_tag: str, dockerfile: str,
              context_dir: str | None = None) -> dict:
        ctx = context_dir or "."
        df_path = Path(dockerfile)
        if not df_path.is_absolute():
            df_path = Path(ctx) / df_path
        if not df_path.exists():
            return {"error": f"Dockerfile not found: {df_path}",
                    "image_tag": image_tag, "status": "error"}
        try:
            _img, _logs = self._ensure_client().images.build(
                path=str(df_path.parent),
                dockerfile=str(df_path.name),
                tag=image_tag,
                rm=True,
            )
            return {"image_tag": image_tag, "status": "built"}
        except (docker.errors.BuildError, APIError, OSError) as e:
            return {"error": str(e), "image_tag": image_tag, "status": "error"}

    def suggest_paths(self, name: str, missing_path: str) -> list:
        """Suggest similar paths in the container using ls/find."""
        dirname = str(Path(missing_path).parent)
        basename = Path(missing_path).name
        ls_cmd = (
            f"ls -1 {shlex.quote(dirname)} 2>/dev/null | "
            f"grep -i {shlex.quote(basename)} | head -5"
        )
        result = self.exec_oneoff(name, ls_cmd, timeout=10)
        if result.get("exit_code") not in (0, None) or not result.get("output"):
            return []
        prefix = dirname.rstrip("/")
        return [
            f"{prefix}/{line.strip()}"
            for line in (result["output"] or "").splitlines()
            if line.strip()
        ]

    # ---- shell / exec ----

    def open_shell(self, name: str) -> ShellSession:
        container_name = self._container_name(name)
        return ShellSession(["docker", "exec", "-i", container_name, "bash"])

    def exec_oneoff(self, name: str, command: str, timeout: int = 30,
                    stdin_data: str | None = None) -> dict:
        container_name = self._container_name(name)
        # Use subprocess for oneoff exec because docker SDK's exec_run
        # does not support piping stdin data. The CLI handles stdin
        # via ``-i`` flag with subprocess.run(input=...).
        try:
            result = subprocess.run(
                ["docker", "exec", "-i", container_name, "bash", "-c", command],
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "exit_code": result.returncode,
                "output": result.stdout or "",
                "stderr": result.stderr or "",
            }
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "output": "", "stderr": "timeout"}
