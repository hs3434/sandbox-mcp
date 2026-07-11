"""Docker backend: manages containers via the docker Python SDK.

Lifecycle operations (create, stop, start, remove, commit, build) use
the SDK's structured API: no shell-command construction, no stderr
parsing, no ARG_MAX limits.

Shell operations (open_shell, exec_oneoff) also use the SDK so that
they work correctly when the Docker daemon is on a different host
(the docker CLI ``subprocess`` approach only works when both are on
the same machine).
"""

from __future__ import annotations

import contextlib
import shlex
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
        volume_bindings: dict = {}
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
            repo, tag_part = ([*tag.rsplit(":", 1), ""])[:2]
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
            self._ensure_client().images.build(
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
        try:
            container = self._ensure_client().containers.get(container_name)
        except NotFound:
            return {"exit_code": -1, "output": "", "stderr": "container not found"}

        if stdin_data is None:
            try:
                exit_code, output = container.exec_run(
                    cmd=["bash", "-c", command],
                    stdout=True, stderr=True, demux=False,
                )
            except APIError as e:
                return {"exit_code": -1, "output": "",
                        "stderr": str(e.explanation or e)}
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return {"exit_code": exit_code, "output": output or "", "stderr": ""}

        # stdin_data path: socket-based exec via the low-level Docker API.
        # Docker's multiplexed-stream protocol prefixes each chunk with
        # an 8-byte frame header: stream_type(1) + padding(3) + payload_len(uint32 BE).
        import socket as _socket
        import struct

        try:
            exec_id = self._ensure_client().api.exec_create(
                container.id,
                ["bash", "-c", command],
                stdin=True, stdout=True, stderr=True,
            )["Id"]
            sock = self._ensure_client().api.exec_start(
                exec_id, detach=False, socket=True,
            )
        except APIError as e:
            return {"exit_code": -1, "output": "",
                    "stderr": str(e.explanation or e)}

        try:
            encoded = stdin_data.encode("utf-8")
            sock._sock.sendall(encoded)
            sock._sock.shutdown(_socket.SHUT_WR)

            stdout_data = b""
            stderr_data = b""
            sock._sock.settimeout(timeout)

            while True:
                header = b""
                try:
                    while len(header) < 8:
                        chunk = sock._sock.recv(8 - len(header))
                        if not chunk:
                            break
                        header += chunk
                except TimeoutError:
                    break
                except OSError:
                    break
                if len(header) < 8:
                    break

                payload_len = struct.unpack(">I", header[4:8])[0]
                payload = b""
                try:
                    while len(payload) < payload_len:
                        chunk = sock._sock.recv(payload_len - len(payload))
                        if not chunk:
                            break
                        payload += chunk
                except TimeoutError:
                    break

                if header[0] == 1:
                    stdout_data += payload
                elif header[0] == 2:
                    stderr_data += payload

            info = self._ensure_client().api.exec_inspect(exec_id)
            exit_code = info.get("ExitCode")
        finally:
            with contextlib.suppress(Exception):
                sock.close()

        return {
            "exit_code": exit_code,
            "output": stdout_data.decode("utf-8", errors="replace"),
            "stderr": stderr_data.decode("utf-8", errors="replace"),
        }
