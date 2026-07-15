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
        """Return a cached DockerClient, building one on first use.

        Connection source, in priority order:
          1. ``[docker] host`` / ``tls_verify`` / ``cert_path`` in config
             (overridable via ``$DOCKER_HOST`` etc. via ``_apply_env_overrides``).
          2. The standard docker SDK env vars (``from_env()``), including
             ``~/.docker/config.json`` contexts.
          3. The SDK's default: ``unix:///var/run/docker.sock``.

        Set ``[docker] host = "tcp://host:2376"`` (with ``tls_verify=true``
        and ``cert_path=...``) for a remote TLS-protected daemon, or
        ``ssh://user@host`` to ride an existing SSH trust path.
        """
        if self._client is None:
            docker = _docker_module()
            docker_cfg = _load_config().docker
            if docker_cfg.host:
                self._client = docker.DockerClient(
                    base_url=docker_cfg.host,
                    tls=docker_cfg.tls_verify or None,
                    cert=docker_cfg.cert_path or None,
                )
            else:
                self._client = docker.from_env()
        return self._client

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
        # Note: agent-supplied ``volumes`` mounts are intentionally NOT
        # accepted.  The only bind mount is the auto-attached work_home
        # below — agents that need additional paths must ``docker exec``
        # into the container or extend the sandbox.  This keeps the
        # sandbox's host-filesystem boundary intact: the agent cannot
        # smuggle arbitrary host directories (e.g. ``/etc``, ``/root``)
        # into a sandboxed container.
        #
        # Likewise, agent-supplied ``ports``, ``env``, and ``workdir`` are
        # not honoured: inter-container access uses the auto-created
        # bridge network by container-name DNS, exposing host port mappings
        # would undo the sandbox's network isolation, the container's
        # working directory is fixed at /workspace (the auto-mounted
        # workspace; agents can `cd` inside any shell).  See
        # sandbox_env.docker_run description for the agent-facing rationale.

        # Container names are the bare machine name.  Namespace is
        # enforced by the ``sandbox-mcp.managed=true`` docker label,
        # set on every container this backend creates (see below).
        container_name = name

        # Ensure shared network for DNS-resolvable container names.
        auto_network = docker_cfg.auto_network
        if auto_network:
            self.ensure_network(auto_network)

        # Auto-create a persistent workspace directory on the host and mount
        # it to /workspace inside the container.  The agent never sees the
        # host path — it just works in /workspace.
        machine_dir = get_work_dir(name)
        volume_bindings: dict = {machine_dir: {"bind": "/workspace", "mode": "rw"}}

        # Reconciliation labels: identify containers this backend owns so
        # :meth:`list_managed_containers` can re-discover them after the
        # server restarts.  ``sandbox-mcp.managed`` is the authoritative
        # marker - the prefix alone is too soft (an attacker could create
        # a ``sandbox-foo`` container by hand and the prefix check would
        # happily pick it up).  ``sandbox-mcp.purpose`` persists the
        # machine's purpose across restarts (docker labels are immutable
        # post-creation, so changing purpose means recreating the
        # container -- there is deliberately no in-place update path).
        labels = {
            "sandbox-mcp.managed": "true",
            "sandbox-mcp.machine": name,
        }
        if purpose:
            labels["sandbox-mcp.purpose"] = purpose

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
                working_dir="/workspace",
                volumes=volume_bindings if volume_bindings else None,
                network=auto_network or None,
                command="sleep infinity",
                labels=labels,
            )
        except _docker_module().errors.ImageNotFound:
            # ImageNotFound is a subclass of APIError - it MUST be caught
            # first, otherwise the APIError handler below swallows it and
            # the specific "image not found" message is lost.
            return TargetInfo(
                name=name,
                backend="docker",
                status="error",
                purpose=purpose,
                error=f"Image {image} not found",
            )
        except _docker_module().errors.APIError as e:
            # HTTP 409 Conflict == a container with this name already
            # exists (e.g. the agent called docker_run twice in one
            # session, or the daemon still has the container from a
            # previous run that docker_ps reconciliation didn't see).
            # Reattach to it instead of failing - this is the
            # idempotent "reattach" behaviour documented for docker_run.
            if getattr(e, "status_code", None) == 409:
                reattached = self._reattach_existing(name, purpose, image)
                if reattached is not None:
                    return reattached
            return TargetInfo(
                name=name,
                backend="docker",
                status="error",
                purpose=purpose,
                error=str(e.explanation or e),
            )

        self._started_at[name] = time.time()
        # ``created`` is filled in lazily by ``get_info`` (it queries the
        # daemon for accurate timestamps).  Mark the machine as running
        # so the dispatcher can surface it in ``docker_ps`` immediately.
        return TargetInfo(
            name=name,
            backend="docker",
            status="running",
            purpose=purpose,
            image=image,
        )

    def _reattach_existing(self, name: str, purpose: str, image: str) -> TargetInfo | None:
        """Adopt an already-existing container after a name-conflict (409).

        Called from :meth:`create` when ``containers.run`` reports the
        name is taken.  Looks the container up, starts it if it isn't
        running, and returns a :class:`TargetInfo` reflecting its TRUE
        state.  Returns ``None`` if the container vanished between the
        conflict and the lookup (a race) so the caller can surface the
        original error.

        The container's labels/volumes are trusted as-is: it was either
        created by a previous ``create()`` call (same labels) or already
        reconciled by ``docker_ps``.  We do not reconfigure it.

        The returned info carries a ``note`` ("reattached to existing
        container ...") so the agent knows this was a reuse, not a fresh
        create, and that prior filesystem state may have been preserved.

        Docker labels are immutable post-creation, so a reattach CANNOT
        adopt a new ``purpose``: the existing container's
        ``sandbox-mcp.purpose`` label is the truth.  If the caller passed
        a different non-empty purpose, a note flags it (the agent must
        remove + recreate to change purpose).  The returned TargetInfo
        carries the EXISTING purpose, not the caller's.
        """
        docker_errors = _docker_module().errors
        try:
            container = self._ensure_client().containers.get(name)
        except docker_errors.NotFound:
            return None
        existing_purpose = (container.labels or {}).get("sandbox-mcp.purpose", "")
        status = container.attrs.get("State", {}).get("Status", "unknown")
        if status != "running":
            try:
                container.start()
            except docker_errors.APIError as e:
                return TargetInfo(
                    name=name,
                    backend="docker",
                    status="error",
                    purpose=existing_purpose,
                    image=image,
                    error=f"container exists but failed to start: {e}",
                )
            note = "reattached to existing container (started; was stopped)"
        else:
            note = "reattached to existing container (already running)"
        if purpose and purpose != existing_purpose:
            note += (
                f"; passed purpose {purpose!r} ignored (existing has "
                f"{existing_purpose!r}); remove+recreate to change purpose"
            )
        return self._running_info(container, name, purpose=existing_purpose, image=image, note=note)

    def _running_info(
        self, container, name: str, purpose: str = "", image: str = "", note: str = ""
    ) -> TargetInfo:
        """Build a TargetInfo reflecting the container's TRUE state after a
        start attempt.

        ``container.start()`` (and ``containers.run(detach=True)``) return
        as soon as the start request is accepted -- they do NOT wait for
        the container's process to keep running.  A container whose
        command crashes (bad image, missing binary, OOM) enters
        "exited"/"dead" within milliseconds.  Blindly reporting
        "running" would mislead the agent into shelling into a dead
        container.

        This reloads the container's attrs and inspects ``State``: if
        running, returns a ``running`` info (carrying ``note``);
        otherwise returns an ``error`` info with a diagnostic (status,
        exit code, and a short tail of the container's logs) so the
        operator can see *why* it won't run.
        """
        docker_errors = _docker_module().errors
        # attrs may be stale if reload fails; inspect whatever we have.
        with contextlib.suppress(docker_errors.APIError):
            container.reload()
        state = (container.attrs or {}).get("State", {}) or {}
        status = state.get("Status", "unknown")
        if status == "running":
            self._started_at[name] = time.time()
            return TargetInfo(
                name=name,
                backend="docker",
                status="running",
                purpose=purpose,
                image=image,
                note=note,
            )
        # Not running -- assemble a diagnostic hint.
        parts = [f"container is {status!r} after start"]
        exit_code = state.get("ExitCode")
        if exit_code not in (None, 0):
            parts.append(f"exit_code={exit_code}")
        err = state.get("Error")
        if err:
            parts.append(f"error={err!r}")
        try:
            tail = container.logs(tail=20)
            if isinstance(tail, bytes):
                tail = tail.decode("utf-8", "replace")
            tail = tail.strip()
            if tail:
                parts.append("last logs:\n" + tail[:1000])
        except Exception:
            pass
        return TargetInfo(
            name=name,
            backend="docker",
            status="error",
            purpose=purpose,
            image=image,
            error="; ".join(parts),
        )

    def start(self, name: str) -> TargetInfo:
        docker = _docker_module()
        container_name = name
        try:
            container = self._ensure_client().containers.get(container_name)
            container.start()
        except (docker.errors.APIError, docker.errors.NotFound) as e:
            return TargetInfo(name=name, backend="docker", status="error", error=str(e))
        # Verify the container is actually running: start() returns as
        # soon as the request is accepted, but a crashing command exits
        # immediately.  Report the real state + a diagnostic if it died.
        return self._running_info(container, name)

    def stop(self, name: str) -> TargetInfo:
        docker = _docker_module()
        container_name = name
        try:
            container = self._ensure_client().containers.get(container_name)
            container.stop(timeout=10)
            return TargetInfo(name=name, backend="docker", status="stopped")
        except (docker.errors.APIError, docker.errors.NotFound) as e:
            return TargetInfo(name=name, backend="docker", status="error", error=str(e))

    def remove(self, name: str) -> dict:
        docker = _docker_module()
        container_name = name
        try:
            container = self._ensure_client().containers.get(container_name)
            container.remove(force=True)
            self._started_at.pop(name, None)
            return {"target": name, "status": "removed"}
        except (docker.errors.APIError, docker.errors.NotFound) as e:
            return {"target": name, "status": "error", "error": str(e)}

    def get_info(self, name: str) -> TargetInfo:
        docker = _docker_module()
        container_name = name
        try:
            container = self._ensure_client().containers.get(container_name)
            status = container.attrs.get("State", {}).get("Status", "unknown")
            running = status == "running"
            image = (
                container.image.tags[0]
                if container.image.tags
                else (container.image.short_id or "")
            )
            purpose = (container.labels or {}).get("sandbox-mcp.purpose", "")
            return TargetInfo(
                name=name,
                backend="docker",
                status="running" if running else "stopped",
                purpose=purpose,
                image=image,
                created=container.attrs.get("Created", ""),
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
        container_name = name
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
        """Build a Docker image from a Dockerfile the agent has already
        written into a sandboxed container's ``/workspace/`` (via
        :func:`sandbox_file_write`).  The bind mount surfaces those files
        on the host at ``work_home/<machine>/``; this method translates
        the container paths back to host paths and runs ``docker build``.

        ``dockerfile`` and ``context_dir`` must both be under
        ``/workspace/`` — host paths are not accepted (sandbox boundary).

        ``dockerfile_content`` is intentionally NOT accepted.  Inline
        mode used to stage the Dockerfile under ``work_home/_builds/``
        and feed it directly to ``docker build`` — bypassing the
        sandbox's file-write audit trail AND dodging the work_home
        visibility check.  A malicious inline Dockerfile
        (``RUN --mount=type=bind,source=/,...``) executes in a
        daemon-orchestrated container with full host kernel
        capabilities, so inline mode is a host-RCE vector.  Use file
        mode instead.
        """
        docker = _docker_module()

        # Inline mode removed for security — see docstring.
        if dockerfile_content is not None:
            return {
                "error": (
                    "dockerfile_content is not supported: write the Dockerfile "
                    "via sandbox_file_write into /workspace/Dockerfile first, "
                    "then call docker_build without dockerfile_content."
                ),
                "image_tag": image_tag,
                "status": "error",
            }

        # File mode — translate container paths to host paths.
        if machine is None:
            return {
                "error": "machine is required (file mode only)",
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

    def list_managed_containers(self) -> list[tuple[str, dict]]:
        """Re-discover containers this backend owns at server startup.

        Returns ``[(machine_name, container_attrs), ...]`` for every
        container carrying the ``sandbox-mcp.managed=true`` label.
        Used by :class:`SandboxServer`'s startup reconciliation pass
        so in-memory state survives restarts.

        The label is authoritative; the name prefix is not consulted.
        A container with ``sandbox-foo`` as its name but no label is
        NOT adopted — it stays an unmanaged host container.
        """
        docker = _docker_module()
        try:
            containers = self._ensure_client().containers.list(
                all=True, filters={"label": "sandbox-mcp.managed=true"}
            )
        except docker.errors.APIError:
            return []
        out: list[tuple[str, dict]] = []
        for c in containers:
            machine = c.labels.get("sandbox-mcp.machine") if c.labels else None
            if not machine:
                continue
            out.append((machine, c.attrs))
        return out

    def list_images(self) -> list[dict]:
        """Return all images on the daemon.

        ``docker_images`` is read-only — an over-broad list leaks
        information but cannot affect production.  Stop/remove/exec
        are the production-impacting operations and they are protected
        by the registry + label gates.
        """
        docker = _docker_module()
        try:
            images = self._ensure_client().images.list(all=True)
        except docker.errors.APIError:
            return []
        result = []
        for img in images:
            short_id = img.short_id
            tags = img.tags if img.tags else [f"<none>:{short_id}"]
            for tag in tags:
                result.append(
                    {
                        "tag": tag,
                        "image_id": short_id,
                        "created": img.attrs.get("Created", ""),
                        "size_mb": round(img.attrs.get("Size", 0) / (1024 * 1024), 1),
                    }
                )
        return result

    # ---- shell / exec ----

    def open_shell(self, name: str) -> ShellSession:
        docker = _docker_module()
        container_name = name
        try:
            container = self._ensure_client().containers.get(container_name)
        except docker.errors.NotFound as e:
            raise RuntimeError(f"Container {container_name} not found") from e
        process = DockerExecProcess(container, ["bash"])
        return ShellSession(process=process)

    def exec_oneoff(self, name: str, command: str, timeout: int = 30) -> dict:
        docker = _docker_module()
        container_name = name
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
        container_name = name
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
