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


def _docker_error(e: Exception) -> dict:
    """Translate a docker SDK exception to the standard MCP error dict.

    Used at the catch sites that just want to surface the daemon's
    explanation as a generic ``{"status": "error", "error": "..."}``
    response.  Call sites that need a richer shape (e.g. ``exec_oneoff``
    with its ``exit_code``/``stderr`` fields, or ``write_file`` with
    its ``stage`` field) keep their own translation.
    """
    return {"status": "error", "error": str(e.explanation or e)}


# Bind-mount targets inside the container.  Single source of truth so a
# future rename only touches this file (the agent-facing schema, tests,
# and READMEs all mirror these constants via grep-and-replace).
_WORKSPACE_BIND = "/workspace"
_SHARE_BIND = "/share"


def _container_to_host(container_path: str, machine: str) -> Path:
    """Translate a path under ``/workspace/`` to its host bind-mount location.

    The agent only knows container paths.  sandbox-mcp mounts the host's
    ``work_home/<machine>/`` into the container at ``/workspace``, so any
    file the agent wrote via :func:`sandbox_file_write` lives at the
    translated host path on the operator's filesystem.

    Anything outside ``/workspace/`` is rejected — two reasons: (a) host
    paths would break the sandbox boundary, and (b) ``/workspace/`` is
    the ONLY bind-mount into the container, so other container paths
    (e.g. ``/etc/foo``) live in the container's overlay FS only and the
    docker daemon (running on the host) cannot read them.  The agent
    sees those files fine via ``shell_exec``, but ``docker build`` (and
    every other path-translating tool) will reject them.
    """
    if container_path != "/workspace" and not container_path.startswith("/workspace/"):
        raise ValueError(f"path must be under /workspace (sandbox boundary): {container_path!r}")
    rel = container_path[len("/workspace/") :] if container_path != "/workspace" else ""
    target = get_work_home() / machine / rel
    return target


def _curated_container_view(container, name: str) -> dict:
    """Build the curated container view returned by :meth:`inspect`.

    Kept module-level so the public method stays a thin wrapper around
    a pure function — easier to test and to keep the response schema
    in one place.
    """
    attrs = container.attrs or {}
    state = attrs.get("State") or {}
    config = attrs.get("Config") or {}
    host_cfg = attrs.get("HostConfig") or {}
    restart = host_cfg.get("RestartPolicy") or {}
    health = state.get("Health") or {}

    return {
        "id": container.short_id,
        "name": name,
        "image": config.get("Image", ""),
        "created": attrs.get("Created", ""),
        "started_at": state.get("StartedAt", ""),
        "finished_at": state.get("FinishedAt", ""),
        "state": {
            "status": state.get("Status", "unknown"),
            "running": bool(state.get("Running", False)),
            "exit_code": state.get("ExitCode", 0),
            "error": state.get("Error", "") or "",
            "restart_count": state.get("RestartCount", 0),
            "health": health.get("Status", "") or "",
            "oom_killed": bool(state.get("OOMKilled", False)),
            "dead": bool(state.get("Dead", False)),
            "pid": state.get("Pid") or 0,
        },
        "cmd": list(config.get("Cmd") or []) or None,
        "entrypoint": list(config.get("Entrypoint") or []) or None,
        "mounts": [
            {
                "source": m.get("Source", ""),
                "destination": m.get("Destination", ""),
                "mode": m.get("Mode", ""),
            }
            for m in (attrs.get("Mounts") or [])
        ],
        "labels": dict(config.get("Labels") or {}),
        "restart_policy": {
            "name": restart.get("Name", ""),
            "max_retry": restart.get("MaximumRetryCount", 0),
        },
    }


def _curated_image_view(image) -> dict:
    """Build the curated image view returned by :meth:`inspect`.

    Exposes identity + build-time config.  Env values are redacted to
    keys only (agents should use ``shell_exec`` for runtime env).  Volume
    definitions are kept because they're part of the image contract.
    """
    attrs = image.attrs or {}
    config = attrs.get("Config") or {}

    return {
        "id": image.short_id,
        "tags": list(image.tags) if image.tags else [],
        "created": attrs.get("Created", ""),
        "size_bytes": attrs.get("Size", 0),
        "architecture": attrs.get("Architecture", ""),
        "os": attrs.get("Os", ""),
        "cmd": list(config.get("Cmd") or []) or None,
        "entrypoint": list(config.get("Entrypoint") or []) or None,
        "env_keys": sorted(k.split("=", 1)[0] for k in (config.get("Env") or []) if "=" in k),
        "exposed_ports": sorted((config.get("ExposedPorts") or {}).keys()),
        "volumes": sorted((config.get("Volumes") or {}).keys()),
        "labels": dict(config.get("Labels") or {}),
        "working_dir": config.get("WorkingDir", "") or None,
        "user": config.get("User", "") or None,
    }


def _build_share_bindings(name: str) -> dict:
    """Build bind mounts for the inter-container share dir.

    Two mounts, fixed regardless of peer count:

    1. The whole share root is bind-mounted **read-only** at
       ``/share/``.  The kernel evaluates a mount's contents at
       access time, so peer subdirectories
       created *after* a container starts are visible to it on the
       next ``ls`` — no remount needed.
    2. The container's own subdirectory is overlaid **read-write** at
       ``/share/<name>/``, so the agent can drop artefacts for peers
       to read while still being unable to tamper with peer
       subdirectories (the parent mount is ro; only the overlay path
       is writable).

    Returns a dict of ``{host_path: {"bind": container_path, "mode": ...}}``
    ready for the docker SDK's ``volumes=`` kwarg.  Empty when
    ``[storage] share_subdir`` is the empty string (feature disabled).

    Skipped entirely for the admin machine — its ``/host`` mount (the
    whole ``work_home``) already exposes ``work_home/<share_subdir>/``
    at ``/host/<share_subdir>/``.
    """
    share_subdir = _load_config().storage.share_subdir
    if not share_subdir:
        return {}
    share_root = get_work_home() / share_subdir
    self_dir = share_root / name
    share_root.mkdir(parents=True, exist_ok=True)
    self_dir.mkdir(parents=True, exist_ok=True)
    return {
        str(share_root.resolve()): {"bind": _SHARE_BIND, "mode": "ro"},
        str(self_dir.resolve()): {
            "bind": f"{_SHARE_BIND}/{name}",
            "mode": "rw",
        },
    }


def _tail_build_log(build_log, max_entries=5):
    """Extract the last *max_entries* lines of a docker BuildError log.

    The log is a list of dicts (one per build step), where each dict
    may have ``"stream"``, ``"error"``, ``"errorDetail"``, etc. as keys.
    Returns an empty string when the log is empty or can't be read.
    """
    if not build_log:
        return ""
    try:
        tail = list(build_log)[-max_entries:]
        parts = []
        for entry in tail:
            if isinstance(entry, dict):
                line = entry.get("error") or entry.get("stream") or ""
                # Trim trailing whitespace to avoid spilling metadata.
                parts.append(str(line).strip() if line else str(entry))
            else:
                parts.append(str(entry))
        return "; ".join(p.strip() for p in parts if p.strip())
    except Exception:
        return ""


class DockerExecProcess:
    """Wrapper around a Docker SDK exec socket that mimics ``subprocess.Popen``.

    This allows :class:`ShellSession` to use an SDK-based persistent exec
    (created via the Docker API) instead of a ``docker exec -i`` subprocess,
    so that ``open_shell`` works correctly when the Docker daemon is on a
    different host.
    """

    def __init__(self, container, cmd):
        self._container = container
        exec_id = container.client.api.exec_create(
            container.id,
            cmd,
            stdin=True,
            stdout=True,
            stderr=True,
        )["Id"]
        # Private form used by exec_inspect / kill paths; public alias so
        # ShellSession.bash_pid can surface a stable process identifier.
        self._exec_id = exec_id
        self.exec_id = exec_id
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
        # If either thread fails to start (very rare — system resource
        # exhaustion), close everything we've built so the caller doesn't
        # inherit leaked pipe fds.  Same on any exception raised here.
        try:
            self._demux_thread.start()
            self._stdin_thread.start()
        except Exception:
            self._cleanup_pipes_on_init_failure()
            raise

    def _cleanup_pipes_on_init_failure(self):
        """Close all pipe fds if __init__ aborted partway.  Best-effort."""
        for fd_name in ("_stdin_r_fd", "_stdin_w_fd", "_stdout_r_fd", "_stdout_w_fd"):
            fd = getattr(self, fd_name, None)
            if fd is None:
                continue
            with contextlib.suppress(OSError):
                os.close(fd)
            setattr(self, fd_name, None)
        for attr in ("stdin", "stdout"):
            obj = getattr(self, attr, None)
            if obj is not None:
                with contextlib.suppress(Exception):
                    obj.close()

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
        self._shell: dict[str, str] = {}

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
        is_admin = bool(docker_cfg.admin_machine) and name == docker_cfg.admin_machine
        # Image: explicit kwarg > default_image.  Admin has no separate image;
        # pass image= to docker_run(name=admin, image=...) when you need a
        # different toolchain for it.
        image = kwargs.get("image", docker_cfg.default_image)
        # Note: agent-supplied ``volumes`` mounts are intentionally NOT
        # accepted.  The only bind mounts are:
        #   (1) ``work_home/<name>`` → ``/workspace`` (the per-machine
        #       workspace, rw), and
        #   (2) the auto-discovered shared directory
        #       ``work_home/<share_subdir>/<name>`` → ``/share/<name>``
        #       (rw) plus every peer subdirectory read-only — see
        #       ``_build_share_bindings``.
        #
        # EXCEPTION — the admin machine (name == ``docker.admin_machine``,
        # non-empty): it gets an ADDITIONAL mount
        #   ``work_home`` → ``/host`` (rw, global view).
        # The whole work_home tree is exposed so the agent can read every
        # peer's workspace and modify them when doing cross-machine
        # cleanup.  Admin is its own god-mode container — operations there
        # are irreversible.  Share bindings are skipped because the global
        # mount already covers ``work_home/<share_subdir>/``.
        #
        # Arbitrary host paths (``/etc``, ``/root``, the docker socket)
        # remain unreachable from inside the container: an agent cannot
        # smuggle them via any sandbox-mcp tool.
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

        # Ensure shared network for DNS-resolvable container names.
        auto_network = docker_cfg.auto_network
        if auto_network:
            self.ensure_network(auto_network)

        # Auto-create a persistent workspace directory on the host and mount
        # it to /workspace inside the container.  The agent never sees the
        # host path — it just works in /workspace.
        machine_dir = get_work_dir(name)
        volume_bindings: dict = {machine_dir: {"bind": _WORKSPACE_BIND, "mode": "rw"}}

        if is_admin:
            # Global view: admin sees the entire work_home tree at /host.
            # Overlap with /workspace (both reach work_home/<admin>/) is
            # intentional and harmless — writes through either path hit
            # the same inodes on the host.
            volume_bindings[get_work_home()] = {"bind": "/host", "mode": "rw"}
        else:
            # Build the inter-container share mount spec for peers only.
            # Failures here (e.g. permission errors creating the share
            # dir) are returned to the agent BEFORE touching the daemon,
            # so a bad config never leaves a half-created container behind.
            try:
                share_bindings = _build_share_bindings(name)
            except OSError as e:
                return TargetInfo(
                    name=name,
                    backend="docker",
                    status="error",
                    purpose=purpose,
                    error=f"failed to set up share dir: {e}",
                )
            volume_bindings.update(share_bindings)

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
                name=name,
                init=True,
                restart_policy={
                    "Name": docker_cfg.restart_policy_name,
                    "MaximumRetryCount": docker_cfg.restart_max_retry_count,
                },
                working_dir=_WORKSPACE_BIND,
                volumes=volume_bindings if volume_bindings else None,
                network=auto_network or None,
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
                reattached = self._reattach_existing(
                    name, purpose, image, shell=kwargs.get("shell", "bash")
                )
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
        self._shell[name] = kwargs.get("shell", "bash")
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

    def _reattach_existing(
        self, name: str, purpose: str, image: str, shell: str = "bash"
    ) -> TargetInfo | None:
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

        ``shell`` is the user-facing exec shell (defaults to ``bash``);
        reattach adopts the caller's choice so subsequent ``docker exec``
        calls match what they would have been on a fresh create.
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
        self._shell[name] = shell
        return self._running_info(container, name, purpose=existing_purpose, image=image, note=note)

    def _running_info(
        self,
        container,
        name: str,
        purpose: str = "",
        image: str = "",
        note: str = "",
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

        **Verification model**: a single ``container.reload()`` immediately
        after ``start()`` returns, then a single read of ``State.Status``.
        This is **not** polling — there is no wait, no interval, no max
        timeout.  If the process's crash window (start-accepted →
        process-exit) is shorter than the reload round-trip, the check
        catches it; if the process takes longer to die (e.g. runs a
        short-lived init script, then exits 200ms in), this reports
        "running" for a container that will shortly transition to
        "exited".  Callers needing a more robust check should poll
        ``docker_inspect`` themselves with an appropriate delay.

        Returns a ``running`` info (carrying ``note``) on success, or an
        ``error`` info with a diagnostic (status, exit code, short log
        tail) if the container is not running.
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
        try:
            container = self._ensure_client().containers.get(name)
            container.start()
        except (docker.errors.APIError, docker.errors.NotFound) as e:
            return TargetInfo(name=name, backend="docker", status="error", error=str(e))
        # Verify the container is actually running: start() returns as
        # soon as the request is accepted, but a crashing command exits
        # immediately.  Report the real state + a diagnostic if it died.
        return self._running_info(container, name)

    def stop(self, name: str) -> TargetInfo:
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
            container.stop(timeout=10)
            return TargetInfo(name=name, backend="docker", status="stopped")
        except (docker.errors.APIError, docker.errors.NotFound) as e:
            return TargetInfo(name=name, backend="docker", status="error", error=str(e))

    def remove(self, name: str) -> dict:
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
            container.remove(force=True)
            self._started_at.pop(name, None)
            return {"machine": name, "status": "removed"}
        except (docker.errors.APIError, docker.errors.NotFound) as e:
            return {"machine": name, "status": "error", "error": str(e)}

    def get_info(self, name: str) -> TargetInfo:
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
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
        try:
            container = self._ensure_client().containers.get(name)
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

    def inspect(self, name: str, *, kind: str = "container", raw: bool = False) -> dict:
        """Return curated config for a container or image, or full ``attrs`` when ``raw=True``.

        ``kind`` selects the object type (``"container"`` or ``"image"``).
        Defaults to container for backward compatibility — image inspection
        is opt-in to keep existing callers' results stable.

        Container curated view deliberately omits ``Config.Env``,
        ``Config.WorkingDir``, ``Config.User``, and
        ``NetworkSettings.IPAddress`` — agents get those from
        :func:`sandbox_shell_exec` (``env`` / ``pwd`` / ``whoami`` /
        ``hostname -i``).  The curated set focuses on what ``shell_exec``
        cannot answer: state, cmd, mounts, labels, restart policy.

        Image view returns identity + config (cmd, entrypoint, env-keys
        only — values redacted, expose ports, mounted volumes).
        """
        if kind not in ("container", "image"):
            return {
                "error": f"unknown inspect kind: {kind!r} (use 'container' or 'image')",
                "status": "error",
            }
        docker = _docker_module()
        try:
            if kind == "container":
                obj = self._ensure_client().containers.get(name)
                obj.reload()
            else:
                obj = self._ensure_client().images.get(name)
        except (
            docker.errors.NotFound,
            docker.errors.ImageNotFound,
            docker.errors.APIError,
        ) as e:
            return _docker_error(e)

        if raw:
            return obj.attrs

        if kind == "container":
            return _curated_container_view(obj, name)
        return _curated_image_view(obj)

    def logs(
        self,
        name: str,
        *,
        tail: int = 200,
        since: str | None = None,
        until: str | None = None,
        timestamps: bool = False,
    ) -> dict:
        """Read container logs (one-shot, merged stdout+stderr).

        ``tail`` is capped at 10000 to prevent token-bombing a single
        response.  ``since`` / ``until`` accept RFC 3339 timestamps or
        relative durations (``"10m"``, ``"1h"``) — both formats are
        accepted by the docker daemon.

        Works against stopped containers: docker keeps the log buffer
        past exit, which is the primary use case (read why a container
        died).
        """
        if not isinstance(tail, int) or tail < 1 or tail > 10000:
            return {
                "error": f"tail must be between 1 and 10000, got {tail!r}",
                "status": "error",
            }
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return _docker_error(e)

        try:
            raw = container.logs(tail=tail, since=since, until=until, timestamps=timestamps)
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return _docker_error(e)

        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
        # Heuristic: if the daemon returned at least `tail` newlines, the
        # output was clipped (we asked for tail lines and got tail-or-more,
        # so there might be more we didn't get).  Not exact — a tail line
        # without a trailing newline counts as 0 — but it's the best signal
        # we have without round-tripping with the daemon.
        line_count = text.count("\n")
        truncated = line_count >= tail
        return {"logs": text, "truncated": truncated}

    def diff(self, name: str) -> dict:
        """Filesystem changes vs the container's image, grouped A/C/D.

        docker SDK ``Container.diff()`` returns
        ``[{"Path": str, "Kind": int}, ...]`` where Kind is
        ``0=Modified``, ``1=Added``, ``2=Deleted``.
        """
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
            raw = container.diff()
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return _docker_error(e)

        added, changed, deleted = [], [], []
        for entry in raw or []:
            path = entry.get("Path", "")
            kind = entry.get("Kind")
            if kind == 0:
                changed.append(path)
            elif kind == 1:
                added.append(path)
            elif kind == 2:
                deleted.append(path)

        added.sort()
        changed.sort()
        deleted.sort()

        return {
            "changes": {"A": added, "C": changed, "D": deleted},
            "summary": {"added": len(added), "changed": len(changed), "deleted": len(deleted)},
        }

    def history(self, image: str) -> dict:
        """Layer-by-layer build history for a single image.

        Returns one entry per layer (oldest first), each with id (12-char
        prefix), created (epoch seconds), created_by (the Dockerfile RUN /
        COPY / etc. line), size in bytes, and tags.  Mirrors
        ``docker history <image>`` but as structured data.
        """
        docker = _docker_module()
        try:
            img = self._ensure_client().images.get(image)
            raw = img.history()
        except (
            docker.errors.ImageNotFound,
            docker.errors.NotFound,
            docker.errors.APIError,
        ) as e:
            return _docker_error(e)

        layers = [
            {
                # Strip "sha256:" prefix; keep 12 hex chars (matches
                # ``docker.short_id`` and keeps the response token-friendly).
                "id": (entry.get("Id") or "").removeprefix("sha256:")[:12],
                "created": entry.get("Created", 0),
                "created_by": (entry.get("CreatedBy") or "").strip(),
                "size_bytes": entry.get("Size", 0),
                "tags": list(entry.get("Tags") or []),
            }
            for entry in (raw or [])
        ]
        return {
            "image": image,
            "layers": layers,
            "total_size_bytes": sum(layer["size_bytes"] for layer in layers),
            "layer_count": len(layers),
        }

    def stats(self, name: str) -> dict:
        """One-shot resource snapshot.

        The MCP tool-call model is request/response; for live monitoring
        the agent loops on repeated ``docker_stats`` calls.
        """
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
            raw = container.stats(stream=False)
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return _docker_error(e)

        cpu = self._compute_cpu_percent(raw)
        mem = self._compute_memory(raw)
        net = self._compute_network(raw)
        blk = self._compute_block_io(raw)

        return {
            "cpu_percent": cpu,
            "memory": mem,
            "network": net,
            "block_io": blk,
        }

    @staticmethod
    def _compute_cpu_percent(raw: dict) -> float:
        """Standard docker CPU% formula in single-snapshot form.

        ``cpu_delta / system_delta * num_cpus * 100``; returns 0 when
        system_delta is 0 (first sample or zero-elapsed case).
        """
        cpu_stats = raw.get("cpu_stats") or {}
        precpu_stats = raw.get("precpu_stats") or {}
        cpu_usage = cpu_stats.get("cpu_usage") or {}
        precpu_usage = precpu_stats.get("cpu_usage") or {}
        cpu_delta = (cpu_usage.get("total_usage") or 0) - (precpu_usage.get("total_usage") or 0)
        cur_sys = cpu_stats.get("system_cpu_usage") or 0
        prev_sys = precpu_stats.get("system_cpu_usage") or 0
        system_delta = cur_sys - prev_sys
        if system_delta <= 0 or cpu_delta < 0:
            return 0.0
        num_cpus = cpu_stats.get("online_cpus") or len(cpu_usage.get("percpu_usage") or []) or 1
        return (cpu_delta / system_delta) * num_cpus * 100.0

    @staticmethod
    def _compute_memory(raw: dict) -> dict:
        mem = raw.get("memory_stats") or {}
        usage = mem.get("usage") or 0
        limit = mem.get("limit") or 0
        pct = (usage / limit * 100.0) if limit else 0.0
        return {"usage_bytes": usage, "limit_bytes": limit, "usage_percent": pct}

    @staticmethod
    def _compute_network(raw: dict) -> dict:
        nets = raw.get("networks") or {}
        rx = sum((iface.get("rx_bytes") or 0) for iface in nets.values())
        tx = sum((iface.get("tx_bytes") or 0) for iface in nets.values())
        return {"rx_bytes": rx, "tx_bytes": tx}

    @staticmethod
    def _compute_block_io(raw: dict) -> dict:
        blkio = raw.get("blkio_stats") or {}
        entries = blkio.get("io_service_bytes_recursive") or []
        read_bytes = sum((e.get("value") or 0) for e in entries if e.get("op") == "Read")
        write_bytes = sum((e.get("value") or 0) for e in entries if e.get("op") == "Write")
        return {"read_bytes": read_bytes, "write_bytes": write_bytes}

    def restart(self, name: str, timeout: int = 10) -> TargetInfo:
        """Atomic restart: stop then start, then verify the container
        actually stayed up.  A crashing command exits within ms of
        ``restart()`` returning — we re-check ``State.Status`` and
        surface a diagnostic if the container died, matching the
        ``start()`` semantics.
        """
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
            container.restart(timeout=timeout)
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            return TargetInfo(name=name, backend="docker", status="error", error=str(e))

        # Confirm the container actually came back.  Append the operation
        # name to the error message inline so the helper stays simple.
        info = self._running_info(container, name)
        if info.error:
            info = TargetInfo(
                name=info.name,
                backend=info.backend,
                status=info.status,
                purpose=info.purpose,
                image=info.image,
                created=info.created,
                note=info.note,
                error=info.error.replace("after start", "after restart"),
            )
        return info

    def build(
        self,
        image_tag: str,
        *,
        machine: str,
        dockerfile: str = _WORKSPACE_BIND + "/Dockerfile",
        context_dir: str = _WORKSPACE_BIND,
    ) -> dict:
        """Build a Docker image from a Dockerfile the agent has already
        written into a sandboxed container's ``/workspace/`` (via
        :func:`sandbox_file_write`).  The bind mount surfaces those files
        on the host at ``work_home/<machine>/``; this method translates
        the container paths back to host paths and runs ``docker build``.

        ``dockerfile`` and ``context_dir`` must both be **CONTAINER
        paths** under ``/workspace/`` — they are NOT host paths on the
        mcp process filesystem.  Host paths are rejected by the sandbox
        boundary, AND ``/workspace/`` is the only bind-mount into the
        container: any other container path (e.g. ``/etc/foo``) exists
        only in the container's overlay FS and the docker daemon
        (running on the host) cannot read it.  The agent sees those
        files fine via ``shell_exec``, but this method will refuse them.

        Each ``docker_run(machine=...)`` owns its own ``/workspace/`` —
        files written into ``machine=A``'s ``/workspace/`` are NOT
        visible from ``machine=B``.  Mixing machines is a common
        mistake; the ``error_kind`` returned below tells the agent
        which class of mistake it made.

        Existence of the Dockerfile / context is delegated to the Docker
        daemon — the mcp process does NOT pre-check via ``is_file()``
        because that would inspect the mcp container's filesystem, not
        the daemon's.  When sandbox-mcp runs inside a container with a
        docker socket mount, ``work_home`` is a HOST-side path invisible
        to the mcp process; a pre-check there would falsely report
        "not found" for files that the daemon can see fine.

        Inline Dockerfile mode (``dockerfile_content``) was removed for
        security: it bypassed the sandbox's file-write audit trail AND
        dodged the work_home visibility check.  A malicious inline
        Dockerfile (``RUN --mount=type=bind,source=/,...``) executes in
        a daemon-orchestrated container with full host kernel
        capabilities, so inline mode is a host-RCE vector.  Write the
        Dockerfile via :func:`sandbox_file_write` first, then build.

        Error response shape (always includes ``machine`` + ``image_tag``):

        - ``status="error"`` with one of:

          - ``error_kind="bad_path"`` — agent passed a path outside
            ``/workspace/`` (host path or other container path).  Hint
            in the message explains the container-vs-host distinction.
          - ``error_kind="context_invalid"`` — daemon rejected the
            context because it isn't an existing directory.  Almost
            always means the agent never wrote anything into THIS
            machine's ``/workspace/``, e.g. wrote into a different
            container by mistake.
          - ``error_kind="dockerfile_missing"`` — daemon couldn't open
            the Dockerfile within the build context.  Hint points to
            verifying the path matches the ``sandbox_file_write`` call.
          - ``error_kind="base_image_not_found"`` — the ``FROM <image>``
            can't be resolved (typo, private registry needs
            ``docker login``, etc.).
          - ``error_kind="build_failed"`` — everything else (Dockerfile
            syntax error, ``RUN`` failure, ``COPY`` target missing,
            etc.).  The daemon's build log is appended when available.
        """
        docker = _docker_module()
        base = {"image_tag": image_tag, "machine": machine}

        try:
            ctx_host = _container_to_host(context_dir, machine)
            df_host = _container_to_host(dockerfile, machine)
        except ValueError as e:
            return {
                **base,
                "status": "error",
                "error_kind": "bad_path",
                "error": (
                    f"[machine={machine!r}] {e}. NOTE: dockerfile and "
                    f"context_dir must be CONTAINER paths inside "
                    f"machine {machine!r} (paths under {_WORKSPACE_BIND}/), "
                    f"NOT host paths on the mcp process filesystem. "
                    f"Defaults: dockerfile={dockerfile!r}, "
                    f"context_dir={context_dir!r}."
                ),
            }

        try:
            self._ensure_client().images.build(
                path=str(ctx_host),
                dockerfile=str(df_host.name),
                tag=image_tag,
                rm=True,
            )
            return {**base, "status": "built"}
        except TypeError as e:
            return {
                **base,
                "status": "error",
                "error_kind": "context_invalid",
                "error": (
                    f"[machine={machine!r}] docker build context error: "
                    f"{e}. context_dir={context_dir!r} resolved to host "
                    f"path {str(ctx_host)!r} which is not an existing "
                    f"directory. Each docker_run(machine=...) owns its "
                    f"own {_WORKSPACE_BIND}/ — files in machine=A's "
                    f"{_WORKSPACE_BIND}/ are NOT visible from "
                    f"machine=B. Verify with "
                    f"sandbox_file_read(machine={machine!r}, "
                    f"path={dockerfile!r}). "
                    f"NOTE: when sandbox-mcp runs inside a container, "
                    f"work_home MUST be bind-mounted into the mcp "
                    f"container — the docker SDK client tar-walks the "
                    f"build context locally before sending to the "
                    f"daemon. See docker-compose.yml 'Workspace path note'."
                ),
            }
        except docker.errors.ImageNotFound as e:
            return {
                **base,
                "status": "error",
                "error_kind": "base_image_not_found",
                "error": (
                    f"[machine={machine!r}] base image not found: {e}. "
                    f"The FROM <image> in your Dockerfile couldn't be "
                    f"resolved — typo, registry typo, or a private "
                    f"registry that needs `docker login` first."
                ),
            }
        except docker.errors.BuildError as e:
            msg_lower = (e.msg or "").lower()
            log_tail = _tail_build_log(e.build_log)
            if "failed to read dockerfile" in msg_lower or "no such file" in msg_lower:
                kind = "dockerfile_missing"
                hint = (
                    f" Verify {dockerfile!r} was written via "
                    f"sandbox_file_write(machine={machine!r}, "
                    f"path={dockerfile!r})."
                )
            else:
                kind = "build_failed"
                hint = ""
            return {
                **base,
                "status": "error",
                "error_kind": kind,
                "error": (
                    f"[machine={machine!r}] {e.msg or e}{hint}"
                    + (f"  Build log tail: {log_tail}" if log_tail else "")
                ),
            }
        except (docker.errors.APIError, OSError) as e:
            return {
                **base,
                "status": "error",
                "error_kind": "build_failed",
                "error": f"[machine={machine!r}] {e}",
            }

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
        try:
            container = self._ensure_client().containers.get(name)
        except docker.errors.NotFound as e:
            raise RuntimeError(f"Container {name} not found") from e
        process = DockerExecProcess(container, [self._shell.get(name, "bash")])
        return ShellSession(process=process)

    def exec_oneoff(self, name: str, command: str, timeout: int = 30) -> dict:
        docker = _docker_module()
        try:
            container = self._ensure_client().containers.get(name)
        except docker.errors.NotFound:
            return {"exit_code": -1, "output": "", "stderr": "container not found"}
        try:
            exit_code, output = container.exec_run(
                cmd=[self._shell.get(name, "bash"), "-c", command],
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
        try:
            container = self._ensure_client().containers.get(name)
        except docker.errors.NotFound:
            return {"status": "error", "error": "container not found"}

        parent = os.path.dirname(path) or "/"
        # Combine the parent + tmp_dir mkdirs into one round-trip.  Skip
        # the parent mkdir entirely when target is at filesystem root.
        tmp_dir = f"{_load_config().docker.write_tmp_prefix}{uuid.uuid4().hex[:8]}"
        if parent != "/":
            mkdir_both = self.exec_oneoff(
                name,
                f"mkdir -p {shlex.quote(parent)} {shlex.quote(tmp_dir)}",
            )
            if mkdir_both.get("exit_code") not in (0, None):
                return {
                    "status": "error",
                    "stage": "mkdir",
                    "error": mkdir_both.get("stderr") or "mkdir failed",
                }
        else:
            mkdir_tmp = self.exec_oneoff(name, f"mkdir -p {shlex.quote(tmp_dir)}")
            if mkdir_tmp.get("exit_code") not in (0, None):
                return {
                    "status": "error",
                    "stage": "mkdir_tmp",
                    "error": mkdir_tmp.get("stderr") or "mkdir failed",
                }

        buf = io.BytesIO()
        filename = os.path.basename(path)
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        tar_bytes = buf.getvalue()

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

        # Single round-trip for the atomic rename + cleanup: capture mv's
        # exit code, then rm -rf the (now-empty) tmp dir, then return mv's
        # exit code so the caller still sees the rename status.
        rename = self.exec_oneoff(
            name,
            "ec=0; "
            f"mv -f {shlex.quote(tmp_dir + '/' + filename)} {shlex.quote(path)} || ec=$?; "
            f"rm -rf {shlex.quote(tmp_dir)}; "
            "exit $ec",
        )
        if rename.get("exit_code") not in (0, None):
            return {
                "status": "error",
                "stage": "rename",
                "error": rename.get("stderr") or "rename failed",
            }

        return {"status": "ok", "path": path, "bytes_written": len(content)}
