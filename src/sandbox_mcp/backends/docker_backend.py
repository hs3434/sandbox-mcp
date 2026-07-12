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

from sandbox_mcp.backends.base import Backend, TargetInfo
from sandbox_mcp.config import get_work_dir, get_work_home
from sandbox_mcp.config import load as _load_config
from sandbox_mcp.shell_session import ShellSession


def _docker_module():
    """Lazy import of the ``docker`` SDK.  Not installed for SSH-only deployments."""
    import docker

    return docker


def _container_to_host(container_path: str, machine: str) -> Path:
    """Translate a path under ``/workspace/`` to its host bind-mount location.

    The agent only knows container paths.  sandbox-mcp mounts the host's
    ``work_home/<machine>/`` into the container at ``/workspace``, so any
    file the agent wrote via :func:`sandbox_file_write` lives at the
    translated host path on the operator's filesystem.

    Anything outside ``/workspace/`` is rejected — exposing host paths
    to the agent would break the sandbox boundary.
    """
    if container_path != "/workspace" and not container_path.startswith("/workspace/"):
        raise ValueError(f"path must be under /workspace (sandbox boundary): {container_path!r}")
    rel = container_path[len("/workspace/") :] if container_path != "/workspace" else ""
    target = get_work_home() / machine / rel
    return target


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
            container.id,
            cmd,
            stdin=True,
            stdout=True,
            stderr=True,
        )["Id"]
        self._sock = container.client.api.exec_start(
            self._exec_id,
            detach=False,
            socket=True,
        )
        self._raw = self._sock._sock

        # Pipe for stdin: ShellSession writes → pipe → _stdin_loop → sock
        self._stdin_r_fd, self._stdin_w_fd = os.pipe()
        self.stdin = open(self._stdin_w_fd, "wb", buffering=0)
        # Keep the original fd as well so _stdin_loop can os.read on it.

        # Pipe for stdout: _demux_loop reads sock, strips frame, writes → pipe → ShellSession
        self._stdout_r_fd, self._stdout_w_fd = os.pipe()
        self.stdout = open(self._stdout_r_fd, "rb", buffering=0)

        self._demux_thread = threading.Thread(target=self._demux_loop, daemon=True)
        self._stdin_thread = threading.Thread(target=self._stdin_loop, daemon=True)
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
                        # Defense in depth: a broken/truncated socket (or a
                        # test mock that returns a non-bytes object) would
                        # otherwise spin here forever — ``b"" + MagicMock``
                        # returns a MagicMock via __radd__, never raising,
                        # so the inner ``except (OSError, AttributeError)``
                        # never fires.  Bail cleanly so ``_done`` gets set.
                        if not isinstance(chunk, (bytes, bytearray)):
                            return
                        if not chunk:
                            return
                        header += chunk
                except (OSError, AttributeError):
                    return
                payload_len = struct.unpack(">I", header[4:8])[0]
                payload = b""
                try:
                    while len(payload) < payload_len:
                        chunk = sock.read(payload_len - len(payload))
                        if not isinstance(chunk, (bytes, bytearray)):
                            return
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
        # Close the FileIO wrappers first so their __del__ doesn't run
        # against an already-closed fd (which would raise EBADF during GC
        # and surface as a PytestUnraisableExceptionWarning).
        with contextlib.suppress(Exception):
            self.stdin.close()
        with contextlib.suppress(Exception):
            self.stdout.close()
        with contextlib.suppress(OSError):
            os.close(self._stdin_w_fd)
        with contextlib.suppress(OSError):
            os.close(self._stdout_r_fd)
        with contextlib.suppress(Exception):
            self._sock.close()
        self._done.wait(timeout=2)

    def wait(self, timeout=None):
        self._done.wait(timeout=timeout)


class DockerBackend(Backend):
    """Docker container backend powered by ``docker.DockerClient``."""

    def __init__(self):
        self._client = None  # lazy init
        self._started_at: dict[str, float] = {}

    def _ensure_client(self):
        if self._client is None:
            self._client = _docker_module().from_env()
        return self._client

    def _container_name(self, name: str) -> str:
        return f"{_load_config().docker.container_name_prefix}{name}"

    # ---- lifecycle ----

    def ensure_network(self, name: str) -> None:
        """Create the user-defined bridge network ``name`` if it doesn't exist.

        Idempotent: subsequent calls are no-ops.  If ``name`` is empty
        the method returns immediately (no-op mode).
        """
        if not name:
            return
        docker = _docker_module()
        try:
            self._ensure_client().networks.get(name)
        except docker.errors.NotFound:
            with contextlib.suppress(docker.errors.APIError):
                self._ensure_client().networks.create(name, driver="bridge", check_duplicate=True)
        except docker.errors.APIError:
            pass  # daemon unreachable — not fatal (container run will fail later)

    def create(self, name: str, purpose: str = "", **kwargs) -> TargetInfo:
        docker_cfg = _load_config().docker
        image = kwargs.get("image", docker_cfg.default_image)
        volumes = kwargs.get("volumes", []) or []
        ports = kwargs.get("ports", []) or []
        env = kwargs.get("env", {}) or {}
        workdir = kwargs.get("workdir", docker_cfg.default_workdir)

        container_name = self._container_name(name)

        # Ensure shared network for DNS-resolvable container names.
        auto_network = docker_cfg.auto_network
        if auto_network:
            self.ensure_network(auto_network)

        # Auto-create a persistent workspace directory on the host and mount
        # it to /workspace inside the container.  The agent never sees the
        # host path — it just works in /workspace.
        machine_dir = get_work_dir(name)
        volumes = list(volumes)  # copy so we don't mutate the caller's list
        volumes.append(f"{machine_dir}:/workspace")
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
                restart_policy={
                    "Name": docker_cfg.restart_policy_name,
                    "MaximumRetryCount": docker_cfg.restart_max_retry_count,
                },
                working_dir=workdir,
                volumes=volume_bindings if volume_bindings else None,
                ports=port_bindings if port_bindings else None,
                environment=env or None,
                network=auto_network or None,
                command="sleep infinity",
            )
        except _docker_module().errors.APIError as e:
            return TargetInfo(
                name=name,
                backend="docker",
                status="error",
                purpose=purpose,
                error=str(e.explanation or e),
            )
        except _docker_module().errors.ImageNotFound:
            return TargetInfo(
                name=name,
                backend="docker",
                status="error",
                purpose=purpose,
                error=f"Image {image} not found",
            )

        self._started_at[name] = time.time()
        return TargetInfo(name=name, backend="docker", status="running", purpose=purpose)

    def start(self, name: str) -> TargetInfo:
        docker = _docker_module()
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
            container.start()
            self._started_at[name] = self._started_at.get(name, time.time())
            return TargetInfo(name=name, backend="docker", status="running")
        except (docker.errors.APIError, docker.errors.NotFound) as e:
            return TargetInfo(name=name, backend="docker", status="error", error=str(e))

    def stop(self, name: str) -> TargetInfo:
        docker = _docker_module()
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
            container.stop(timeout=10)
            return TargetInfo(name=name, backend="docker", status="stopped")
        except (docker.errors.APIError, docker.errors.NotFound) as e:
            return TargetInfo(name=name, backend="docker", status="error", error=str(e))

    def remove(self, name: str) -> dict:
        docker = _docker_module()
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
            container.remove(force=True)
            self._started_at.pop(name, None)
            return {"target": name, "status": "removed"}
        except (docker.errors.APIError, docker.errors.NotFound) as e:
            return {"target": name, "status": "error", "error": str(e)}

    def get_info(self, name: str) -> TargetInfo:
        docker = _docker_module()
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
            status = container.attrs.get("State", {}).get("Status", "unknown")
            running = status == "running"
            return TargetInfo(
                name=name,
                backend="docker",
                status="running" if running else "stopped",
            )
        except (docker.errors.APIError, docker.errors.NotFound):
            return TargetInfo(name=name, backend="docker", status="error")

    # ---- docker-specific extras ----

    def commit(self, name: str, image_tag: str) -> dict:
        """Commit a container's filesystem state to ``image_tag``.

        ``image_tag`` must be a fully-qualified repo:tag (e.g.
        ``myapp:v1``).  No auto-defaulting — the caller is responsible
        for choosing a tag that won't collide with other machines.
        """
        docker = _docker_module()
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
            repo, tag_part = ([*image_tag.rsplit(":", 1), ""])[:2]
            if not repo or ":" not in image_tag:
                # container.commit() rejects tags without ':' — guard here
                # with a clearer message.
                return {
                    "error": (f"image_tag must be 'repo:tag', got {image_tag!r}"),
                    "image_tag": image_tag,
                    "status": "error",
                }
            container.commit(repository=repo, tag=tag_part or "latest")
            return {"image_tag": image_tag, "status": "committed"}
        except (docker.errors.APIError, docker.errors.NotFound) as e:
            return {"error": str(e), "image_tag": image_tag, "status": "error"}

    def build(
        self,
        image_tag: str,
        *,
        machine: str | None = None,
        dockerfile: str = "/workspace/Dockerfile",
        context_dir: str = "/workspace",
        dockerfile_content: str | None = None,
    ) -> dict:
        """Build a Docker image, sandbox-style.

        Two modes:

        - **Inline** (``dockerfile_content`` provided): the Dockerfile
          is written to a sandbox-mcp-managed temp dir and built from
          there.  No running container required.
        - **File** (default): the agent has already written the
          Dockerfile (and any other context files) into a container's
          ``/workspace/`` via :func:`sandbox_file_write`.  The bind
          mount surfaces them on the host at
          ``work_home/<machine>/``.  This method translates the
          container path back to the host path and runs the build.

        ``dockerfile`` and ``context_dir`` must both be under
        ``/workspace/`` — host paths are not accepted (sandbox boundary).
        """
        docker = _docker_module()

        # Inline mode — no container context needed.
        if dockerfile_content is not None:
            return self._build_inline(image_tag, dockerfile_content)

        # File mode — translate container paths to host paths.
        if machine is None:
            return {
                "error": "machine is required when dockerfile_content is not given",
                "image_tag": image_tag,
                "status": "error",
            }
        try:
            ctx_host = _container_to_host(context_dir, machine)
            df_host = _container_to_host(dockerfile, machine)
        except ValueError as e:
            return {
                "error": str(e),
                "image_tag": image_tag,
                "status": "error",
            }
        if not df_host.is_file():
            return {
                "error": f"Dockerfile not found: {df_host}",
                "image_tag": image_tag,
                "status": "error",
            }
        if not ctx_host.is_dir():
            return {
                "error": f"Build context not found: {ctx_host}",
                "image_tag": image_tag,
                "status": "error",
            }
        try:
            self._ensure_client().images.build(
                path=str(ctx_host),
                dockerfile=str(df_host.name),
                tag=image_tag,
                rm=True,
            )
            return {"image_tag": image_tag, "status": "built"}
        except (docker.errors.BuildError, docker.errors.APIError, OSError) as e:
            return {"error": str(e), "image_tag": image_tag, "status": "error"}

    def _build_inline(self, image_tag: str, dockerfile_content: str) -> dict:
        """Write ``dockerfile_content`` to a temp dir and build from it."""
        import tempfile
        from contextlib import suppress

        docker = _docker_module()
        builds_root = get_work_home() / "_builds"
        builds_root.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="inline-", dir=builds_root))
        try:
            (tmp / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")
            try:
                self._ensure_client().images.build(
                    path=str(tmp),
                    dockerfile="Dockerfile",
                    tag=image_tag,
                    rm=True,
                )
                return {"image_tag": image_tag, "status": "built"}
            except (docker.errors.BuildError, docker.errors.APIError, OSError) as e:
                return {"error": str(e), "image_tag": image_tag, "status": "error"}
        finally:
            with suppress(OSError):
                import shutil

                shutil.rmtree(tmp)

    def suggest_paths(self, name: str, missing_path: str) -> list:
        dirname = str(Path(missing_path).parent)
        basename = Path(missing_path).name
        ls_cmd = (
            f"ls -1 {shlex.quote(dirname)} 2>/dev/null | grep -i {shlex.quote(basename)} | head -5"
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

    # ---- discovery (direct daemon queries, no MachineRegistry) ----

    def list_containers(self, name_prefix: str = "") -> list[dict]:
        docker = _docker_module()
        try:
            containers = self._ensure_client().containers.list(all=True)
        except docker.errors.APIError:
            return []
        result = []
        for c in containers:
            name = c.name
            if name_prefix and not name.startswith(name_prefix):
                continue
            state = c.attrs.get("State", {})
            result.append(
                {
                    "name": name,
                    "status": state.get("Status", "unknown"),
                    "image": c.image.tags[0] if c.image.tags else (c.image.short_id or "unknown"),
                    "created": c.attrs.get("Created", ""),
                }
            )
        result.sort(key=lambda x: x["created"], reverse=True)
        return result

    def list_images(self) -> list[dict]:
        docker = _docker_module()
        try:
            images = self._ensure_client().images.list(all=True)
        except docker.errors.APIError:
            return []
        result = []
        for img in images:
            tags = img.tags if img.tags else [f"<none>:{img.short_id}"]
            for tag in tags:
                result.append(
                    {
                        "tag": tag,
                        "image_id": img.short_id,
                        "created": img.attrs.get("Created", ""),
                        "size_mb": round(img.attrs.get("Size", 0) / (1024 * 1024), 1),
                    }
                )
        return result

    # ---- shell / exec ----

    def open_shell(self, name: str) -> ShellSession:
        docker = _docker_module()
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
        except docker.errors.NotFound as e:
            raise RuntimeError(f"Container {container_name} not found") from e
        process = DockerExecProcess(container, ["bash"])
        return ShellSession(process=process)

    def exec_oneoff(self, name: str, command: str, timeout: int = 30) -> dict:
        docker = _docker_module()
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
        except docker.errors.NotFound:
            return {"exit_code": -1, "output": "", "stderr": "container not found"}
        try:
            exit_code, output = container.exec_run(
                cmd=["bash", "-c", command],
                stdout=True,
                stderr=True,
                demux=False,
            )
        except docker.errors.APIError as e:
            return {"exit_code": -1, "output": "", "stderr": str(e.explanation or e)}
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {"exit_code": exit_code, "output": output or "", "stderr": ""}

    def write_file(self, name: str, path: str, content: bytes) -> dict:
        import io
        import tarfile
        import uuid

        docker = _docker_module()
        container_name = self._container_name(name)
        try:
            container = self._ensure_client().containers.get(container_name)
        except docker.errors.NotFound:
            return {"status": "error", "error": "container not found"}

        parent = os.path.dirname(path) or "/"
        if parent != "/":
            mkdir = self.exec_oneoff(name, f"mkdir -p {shlex.quote(parent)}")
            if mkdir.get("exit_code") not in (0, None):
                return {
                    "status": "error",
                    "stage": "mkdir",
                    "error": mkdir.get("stderr") or "mkdir failed",
                }

        buf = io.BytesIO()
        filename = os.path.basename(path)
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        tar_bytes = buf.getvalue()

        tmp_dir = f"{_load_config().docker.write_tmp_prefix}{uuid.uuid4().hex[:8]}"
        mkdir_tmp = self.exec_oneoff(name, f"mkdir -p {shlex.quote(tmp_dir)}")
        if mkdir_tmp.get("exit_code") not in (0, None):
            return {
                "status": "error",
                "stage": "mkdir_tmp",
                "error": mkdir_tmp.get("stderr") or "mkdir failed",
            }

        try:
            ok = container.put_archive(tmp_dir, tar_bytes)
        except docker.errors.APIError as e:
            self.exec_oneoff(name, f"rm -rf {shlex.quote(tmp_dir)}")
            return {"status": "error", "stage": "put_archive", "error": str(e.explanation or e)}
        if not ok:
            self.exec_oneoff(name, f"rm -rf {shlex.quote(tmp_dir)}")
            return {
                "status": "error",
                "stage": "put_archive",
                "error": "put_archive returned False",
            }

        rename = self.exec_oneoff(
            name,
            f"mv -f {shlex.quote(tmp_dir + '/' + filename)} {shlex.quote(path)}",
        )
        self.exec_oneoff(name, f"rm -rf {shlex.quote(tmp_dir)}")
        if rename.get("exit_code") not in (0, None):
            return {
                "status": "error",
                "stage": "rename",
                "error": rename.get("stderr") or "rename failed",
            }

        return {"status": "ok", "path": path, "bytes_written": len(content)}
