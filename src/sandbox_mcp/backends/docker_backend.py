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
import os
import shlex
import threading
import time
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound, NotFound

from sandbox_mcp.backends.base import Backend, TargetInfo
from sandbox_mcp.shell_session import ShellSession


class DockerExecProcess:
    """Wrapper around a Docker SDK exec socket that mimics ``subprocess.Popen``.

    This allows :class:`ShellSession` to use an SDK-based persistent exec
    (created via the Docker API) instead of a ``docker exec -i`` subprocess,
    so that ``open_shell`` works correctly when the Docker daemon is on a
    different host.
    """

    def __init__(self, container, cmd):
        self._container = container
        self._exec_id = container.client.api.exec_create(
            container.id, cmd, stdin=True, stdout=True, stderr=True,
        )["Id"]
        self._sock = container.client.api.exec_start(
            self._exec_id, detach=False, socket=True,
        )
        self._raw = self._sock._sock

        # Pipe for stdin: ShellSession writes → pipe → _stdin_loop → sock
        self._stdin_r_fd, self._stdin_w_fd = os.pipe()
        self.stdin = open(self._stdin_w_fd, "wb", buffering=0)
        # Keep the original fd as well so _stdin_loop can os.read on it.

        # Pipe for stdout: _demux_loop reads sock, strips frame, writes → pipe → ShellSession
        self._stdout_r_fd, self._stdout_w_fd = os.pipe()
        self.stdout = open(self._stdout_r_fd, "rb", buffering=0)

        self._demux_thread = threading.Thread(
            target=self._demux_loop, daemon=True)
        self._stdin_thread = threading.Thread(
            target=self._stdin_loop, daemon=True)
        self._done = threading.Event()
        self._demux_thread.start()
        self._stdin_thread.start()

    # ---- demux: sock → pipe (reads framed stdout, writes clean bytes) ----

    def _demux_loop(self):
        import struct
        sock = self._sock
        sock._sock.settimeout(300)  # 5 min idle timeout
        out_fd = self._stdout_w_fd
        try:
            while True:
                header = b""
                try:
                    while len(header) < 8:
                        chunk = sock.read(8 - len(header))
                        if not chunk:
                            return
                        header += chunk
                        # Actually use os.read since SocketIO.read can be finicky
                except (OSError, AttributeError):
                    return
                payload_len = struct.unpack(">I", header[4:8])[0]
                payload = b""
                try:
                    while len(payload) < payload_len:
                        chunk = sock.read(payload_len - len(payload))
                        if not chunk:
                            break
                        payload += chunk
                except (OSError, AttributeError):
                    return
                if header[0] == 1:
                    os.write(out_fd, payload)
                # stderr (2) is merged per subprocess.STDOUT convention.
        finally:
            os.close(out_fd)
            self._done.set()

    # ---- stdin pipe → sock ----

    def _stdin_loop(self):
        fd = self._stdin_r_fd
        try:
            while True:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                self._raw.sendall(data)
        finally:
            os.close(fd)

    # ---- Popen-compatible interface ----

    def poll(self):
        info = self._container.client.api.exec_inspect(self._exec_id)
        if info.get("Running", True):
            return None
        return info.get("ExitCode", -1)

    def kill(self):
        # Close the write end of stdin pipe first, so _stdin_loop sees EOF
        # and exits cleanly.
        with contextlib.suppress(OSError):
            os.close(self._stdin_w_fd)
        # Close the stdout pipe: the drain thread will see EOF.
        with contextlib.suppress(OSError):
            os.close(self._stdout_r_fd)
        # Close the docker socket; this causes _demux_loop's sock.read() to
        # return empty, the loop exits, and _done is set.
        with contextlib.suppress(Exception):
            self._sock.close()
        self._done.wait(timeout=2)

    def wait(self, timeout=None):
        self._done.wait(timeout=timeout)


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
        try:
            container = self._ensure_client().containers.get(container_name)
        except NotFound:
            raise RuntimeError(f"Container {container_name} not found")
        process = DockerExecProcess(container, ["bash"])
        return ShellSession(process=process)

    def exec_oneoff(self, name: str, command: str, timeout: int = 30,
                    stdin_data: str | None = None) -> dict:
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
        except NotFound:
            return {"exit_code": -1, "output": "", "stderr": "container not found"}
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
