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

"""env: progressive-discovery environment management.

Default actions advertised via tools/list: help, status.
Discovered via help: machine_list, default_set, shell_new/list/remove.
Discovered via docker_help/ssh_help: backend-specific lifecycle.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any

from sandbox_mcp.backends.base import TargetInfo
from sandbox_mcp.backends.docker_backend import DockerBackend
from sandbox_mcp.backends.ssh_backend import SSHBackend
from sandbox_mcp.config import load as _load_config

logger = logging.getLogger(__name__)

HELP_RESPONSE = {
    "default_actions": [
        {
            "action": "help",
            "summary": "List management actions (summary by default; topic=X for full docs).",
            "description": "Discover common management actions and backend help entries.",
        },
        {
            "action": "status",
            "summary": "Show default machine, machines, and shells.",
            "description": "Show current state: default machine, machine list, shell list.",
        },
    ],
    "operations": [
        {
            "action": "machine_list",
            "summary": "List all registered machines.",
            "description": (
                "List all registered machines with backend, status, "
                "purpose, shell count, and uptime. Lighter than status "
                "(no shell details)."
            ),
            "example": {},
        },
        {
            "action": "default_set",
            "summary": "Set the default machine (machine=X) or default shell (shell_id=X).",
            "description": (
                "Set default machine or default shell. Pass machine to set "
                "the default machine. Pass shell_id to set that shell as "
                "its machine's default shell."
            ),
            "optional": {"machine": "string", "shell_id": "string"},
            "requires": "Exactly one of machine or shell_id",
            "example": {"machine": "dev"},
        },
        {
            "action": "shell_new",
            "summary": "Create an additional shell session on a machine.",
            "description": "Create an additional shell session on a machine.",
            "optional": {"machine": "string", "purpose": "string"},
        },
        {
            "action": "shell_remove",
            "summary": "Terminate and remove a shell session.",
            "description": (
                "Terminate and remove a shell session. If already "
                "terminated, remove the registry entry."
            ),
            "required": {"shell_id": "string"},
        },
        {
            "action": "shell_list",
            "summary": "List all shells, optionally filtered by machine.",
            "description": "List all shells, optionally filtered by machine.",
            "optional": {"machine": "string"},
        },
    ],
    "note": (
        "Core tools are exposed directly: sandbox_shell_exec runs commands "
        "in the machine's persistent session — equivalent to "
        "`docker exec -it <container> bash` on Docker machines and "
        "`ssh <host>` on SSH machines. sandbox_shell_read for non-blocking "
        "output, sandbox_file_read/write/patch/search for files. "
        "Management actions (shell_new, shell_remove, shell_list, "
        "machine_list, default_set) are also top-level tools. "
        "All tools target the default machine unless a [machine] param is "
        "passed. For full action docs call "
        'env(action="help", topic="<action>").'
    ),
}


DOCKER_HELP_RESPONSE = {
    "operations": [
        {
            "action": "docker_run",
            "summary": "Create and start a Docker container (idempotent — reattaches by name).",
            "description": (
                "Create and start a Docker container. Idempotent: "
                "same name re-attaches to existing container. "
                "The container runs the image's CMD/ENTRYPOINT "
                "(e.g. postgres starts as a service); for a generic "
                "exec sandbox use an image with CMD ['sleep', 'infinity']. "
                "Networking: containers on the shared bridge reach each "
                "other by name (e.g. from 'dev' reach 'db' via 'psql -h db'). "
                "Filesystem: /workspace is bind-mounted from the host "
                "(per-machine work_home). "
                "Share: /share/ is bind-mounted; /share/<your-name>/ is rw, "
                "/share/<peer>/ is ro — write artefacts for others under "
                "/share/<your-name>/. "
                "Admin: when name matches [docker] admin_machine, also "
                "mounts /host (rw) for cross-machine inspection."
            ),
            "required": {"name": "string", "image": "string", "purpose": "string"},
            "optional": {
                "shell": (
                    "Shell binary used for ``docker exec`` into this machine "
                    "(default ``bash``). Set to e.g. ``/bin/sh`` for alpine/"
                    "distroless images. Affects interactive shells AND "
                    "one-off command execution (file ops, healthchecks)."
                ),
            },
            "returns": {
                "name": "string",
                "status": "running | error",
                "backend": "docker",
                "note": (
                    "string — only present on 409 reattach (explains reuse, "
                    "purpose-mismatch, etc). On fresh creates and errors the "
                    "field is absent."
                ),
                "error": "string — only present when status='error'",
            },
            "example": {"name": "dev", "image": "python:3.12", "purpose": "Python dev"},
        },
        {
            "action": "docker_ps",
            "summary": "List sandbox-mcp-managed containers (also refreshes the registry).",
            "description": (
                "List sandbox-mcp-managed containers: queries the daemon "
                "for every container carrying the `sandbox-mcp.managed=true` "
                "label and returns their state, image, purpose, and creation "
                "time (direct daemon query, works even after restart when "
                "MachineRegistry is empty). This is also the refresh "
                "operation — each call re-adopts surviving containers into "
                "the registry."
            ),
            "returns": [
                {
                    "name": "string",
                    "status": "string",
                    "image": "string",
                    "purpose": "string",
                    "created": "string",
                }
            ],
        },
        {
            "action": "docker_images",
            "summary": "List available Docker images.",
            "description": "List available Docker images (direct daemon query).",
            "returns": [
                {"tag": "string", "image_id": "string", "created": "string", "size_mb": "number"}
            ],
        },
        {
            "action": "docker_image_history",
            "summary": "Layer-by-layer build history for one image.",
            "description": (
                "Layer-by-layer build history for a single image "
                "(mirrors ``docker history <image>``).  Use this when you "
                "need to inspect one specific image's provenance; use "
                "docker_images when you need to enumerate multiple images."
            ),
            "required": {"image": "string — name:tag, short id, or full id"},
            "returns": {
                "image": "string",
                "layers": [
                    {
                        "id": "string (12-char prefix)",
                        "created": "number (epoch seconds)",
                        "created_by": "string (Dockerfile instruction)",
                        "size_bytes": "number",
                        "tags": "list[string]",
                    }
                ],
                "total_size_bytes": "number",
                "layer_count": "number",
            },
        },
        {
            "action": "docker_build",
            "summary": "Build a Docker image from a Dockerfile in /workspace.",
            "description": (
                "Build a Docker image from a Dockerfile already written "
                "into a sandboxed container's /workspace/ via "
                "file_write.  dockerfile and context_dir MUST both be "
                "CONTAINER paths under /workspace/ — /workspace/ is the "
                "only bind-mount from the host (work_home/<machine>), so "
                "other container paths (e.g. /etc/foo) exist only in the "
                "container's overlay FS and the docker daemon (running "
                "on the host) cannot read them, even though shell_exec "
                "sees them fine.  The will reject non-/workspace/ paths "
                "with error_kind='bad_path'.  "
                "Each docker_run(machine=...) owns its own /workspace/ "
                "— files in machine-A's /workspace/ are NOT visible "
                "from machine-B.  Writing to the wrong container is "
                "common; the build fails with error_kind='context_invalid' "
                "or error_kind='dockerfile_missing' when contexts don't "
                "overlap.  Defaults: dockerfile=/workspace/Dockerfile, "
                "context_dir=/workspace.  Inline dockerfile_content is "
                "not supported (see docker_backend.build docstring for "
                "rationale)."
            ),
            "required": {"image_tag": "string", "machine": "string"},
            "optional": {
                "dockerfile": "string — default /workspace/Dockerfile",
                "context_dir": "string — default /workspace",
            },
            "returns": {
                "image_tag": "string",
                "machine": "string",
                "status": "built | error",
                "error_kind": (
                    "string — only present when status='error'.  One of: "
                    "bad_path (path outside /workspace/), "
                    "context_invalid (context_dir not a directory on host), "
                    "dockerfile_missing (daemon can't open the Dockerfile), "
                    "base_image_not_found (FROM <image> can't be pulled), "
                    "build_failed (syntax error, RUN failure, etc.)."
                ),
                "error": (
                    "string — only present when status='error'.  Prefixed "
                    "with [machine=...] to help diagnose cross-container "
                    "mixing.  Includes daemon build log tail when available."
                ),
            },
        },
        {
            "action": "docker_commit",
            "summary": "Save container state as a new image (image_tag required).",
            "description": (
                "Save container state as a new image.  image_tag is REQUIRED "
                "(format 'repo:tag', e.g. 'myapp:v1') — sandbox-mcp does not "
                "auto-generate one, to prevent silent overwrites when multiple "
                "machines commit."
            ),
            "required": {"machine": "string", "image_tag": "string"},
            "returns": {
                "image_tag": "string",
                "status": "committed | error",
                "error": "string — only present when status='error'",
            },
        },
        {
            "action": "docker_stop",
            "summary": "Stop container (state preserved, can docker_start to resume).",
            "description": ("Stop container. State preserved, can docker_start to resume."),
            "required": {"machine": "string"},
            "returns": {
                "machine": "string",
                "status": "stopped | error",
                "error": "string — only present when status='error'",
            },
        },
        {
            "action": "docker_start",
            "summary": "Start a stopped container.",
            "description": (
                "Start a stopped container.  Verification is a single "
                "post-start state reload — not polling, no timeout, no "
                "interval.  Catches fast crashes (CMD exits within "
                "milliseconds); may report 'running' for containers that "
                "die a moment later.  For robust liveness checks, "
                "subsequent calls to docker_inspect with a delay."
            ),
            "required": {"machine": "string"},
            "returns": {
                "machine": "string",
                "status": "running | error",
                "error": "string — diagnostic with state/exit_code/log tail (on failure)",
            },
        },
        {
            "action": "docker_remove",
            "summary": "Stop and remove container (closes its shells).",
            "description": ("Stop and remove container. Closes all shells for the machine."),
            "required": {"machine": "string"},
            "returns": {
                "machine": "string",
                "status": "removed | error",
                "error": "string — only present when status='error'",
            },
        },
        {
            "action": "docker_inspect",
            "summary": "Curated container or image config (raw=true for full attrs).",
            "description": (
                "Curated config for a container (kind='container', default) "
                "or image (kind='image'). "
                "Container view: state, image, cmd, entrypoint, mounts "
                "(source host path -> bind -> mode), labels, restart "
                "policy.  Deliberately omits Env/working_dir/user/network "
                "(use shell_exec env/pwd/whoami/hostname -i for those). "
                "Image view: identity, tags, size, cmd/entrypoint, env "
                "KEYS ONLY (values redacted — use shell_exec for runtime env), "
                "exposed ports, declared volumes, labels, working_dir, user. "
                "Pass raw=true to get the full attrs dict (incl. Env values, "
                "NetworkSettings, HostConfig)."
            ),
            "required": {"machine": "string"},
            "optional": {
                "kind": (
                    "'container' (default) or 'image'.  Container inspect "
                    "needs a managed machine name; image inspect takes any "
                    "image ref (name:tag, short id, or full id)."
                ),
                "raw": "bool — default false",
            },
            "returns": {
                "container view (default)": {
                    "id": "string (12-char prefix)",
                    "name": "string",
                    "image": "string",
                    "created": "string (ISO 8601)",
                    "started_at": "string (ISO 8601)",
                    "finished_at": "string (ISO 8601)",
                    "state": "object — {status, running, exit_code, error, restart_count, health}",
                    "cmd": "list|null",
                    "entrypoint": "list|null",
                    "mounts": "list of {source, destination, mode}",
                    "labels": "object",
                    "restart_policy": "object — {name, max_retry}",
                },
                "image view (kind='image')": {
                    "id": "string (12-char hex prefix)",
                    "tags": "list[string]",
                    "created": "string (ISO 8601)",
                    "size_bytes": "number",
                    "architecture": "string",
                    "os": "string",
                    "cmd": "list|null",
                    "entrypoint": "list|null",
                    "env_keys": "list[string] — NAMES only, values redacted",
                    "exposed_ports": "list[string]",
                    "volumes": "list[string]",
                    "labels": "object",
                    "working_dir": "string|null",
                    "user": "string|null",
                },
            },
        },
        {
            "action": "docker_logs",
            "summary": "Read container logs (one-shot, merged stdout+stderr).",
            "description": (
                "Read container logs (one-shot, merged stdout+stderr). "
                "Works on stopped containers (primary use: read why a "
                "container died).  tail is hard-capped at 10000."
            ),
            "required": {"machine": "string"},
            "optional": {
                "tail": "int — default 200, max 10000",
                "since": "string — ISO 8601 or relative (e.g. 10m)",
                "until": "string — same format as since",
                "timestamps": "bool — prefix each line with RFC 3339 ts",
            },
            "returns": {"logs": "string", "truncated": "bool"},
        },
        {
            "action": "docker_diff",
            "summary": "Filesystem changes vs the image (A/C/D) — useful before docker_commit.",
            "description": (
                "Filesystem changes vs the container's image, grouped by "
                "kind: A (added), C (changed), D (deleted).  Useful before "
                "docker_commit to confirm 'this is the layer I want to save'."
            ),
            "required": {"machine": "string"},
            "returns": {
                "changes": {"A": "list of paths", "C": "list", "D": "list"},
                "summary": {"added": "int", "changed": "int", "deleted": "int"},
            },
        },
        {
            "action": "docker_stats",
            "summary": "One-shot CPU / memory / network / block IO snapshot.",
            "description": (
                "One-shot resource snapshot: cpu_percent, memory usage/limit, "
                "network rx/tx (aggregated across all interfaces), block IO "
                "read/write.  For live monitoring the agent loops on repeated "
                "docker_stats calls (MCP is request/response)."
            ),
            "required": {"machine": "string"},
            "returns": {
                "cpu_percent": "number",
                "memory": "object",
                "network": "object",
                "block_io": "object",
            },
        },
        {
            "action": "docker_restart",
            "summary": "Atomic restart (stop then start, same post-check as docker_start).",
            "description": (
                "Atomic restart (stop then start) with the same "
                "post-check as docker_start: single post-restart state "
                "reload, not polling.  Catches fast crashes (CMD exits "
                "within milliseconds); may report 'running' for "
                "containers that die a moment later.  For a stopped "
                "container this is equivalent to docker_start."
            ),
            "required": {"machine": "string"},
            "optional": {"timeout": "int — default 10 (seconds for stop phase)"},
            "returns": {
                "machine": "string",
                "status": "running|error",
                "error": "string (on failure)",
            },
        },
    ]
}


SSH_HELP_RESPONSE = {
    "operations": [
        {
            "action": "ssh_connect",
            "summary": "Connect to an SSH remote machine (key auth only).",
            "description": "Connect to an SSH remote machine (key auth only).",
            "required": {"name": "string", "host": "string", "user": "string", "purpose": "string"},
            "optional": {
                "port": "int - default 22",
                "key": "string - private key path (key auth only)",
                "shell": (
                    "Shell binary used for exec into this machine "
                    "(default ``bash``). Set to e.g. ``/bin/sh`` for "
                    "remote hosts without bash."
                ),
            },
            "returns": {
                "name": "string",
                "status": "connected | error",
                "backend": "ssh",
                "error": "string — only present when status='error'",
            },
            "example": {
                "name": "remote",
                "host": "192.168.1.100",
                "user": "ubuntu",
                "purpose": "Remote server",
            },
        },
        {
            "action": "ssh_disconnect",
            "summary": "Close SSH connection (remote machine untouched).",
            "description": "Close SSH connection. Remote machine is not affected.",
            "required": {"machine": "string"},
            "returns": {
                "machine": "string",
                "status": "stopped | error",
                "error": "string — only present when status='error'",
            },
        },
        {
            "action": "ssh_reconnect",
            "summary": "Re-establish SSH connection (shells lost on disconnect).",
            "description": "Re-establish SSH connection. Shells are lost on disconnect.",
            "required": {"machine": "string"},
            "returns": {
                "machine": "string",
                "status": "running | error",
                "error": "string — only present when status='error'",
            },
        },
        {
            "action": "ssh_remove",
            "summary": "Unregister SSH machine (remote machine untouched).",
            "description": "Unregister SSH machine. Remote machine is not affected.",
            "required": {"machine": "string"},
            "returns": {
                "machine": "string",
                "status": "removed | error",
                "error": "string — only present when status='error'",
            },
        },
    ]
}


def _format_uptime(created_at: float) -> str:
    uptime_s = int(time.time() - created_at)
    if uptime_s > 60:
        return f"{uptime_s // 3600}h{(uptime_s % 3600) // 60}m"
    return f"{uptime_s}s"


def _inject_admin_info(name: str, entry: dict) -> None:
    cfg = _load_config()
    if name == cfg.docker.admin_machine:
        entry["admin"] = {
            "host_mount": "/host",
            "warning": ("Operations via /host are irreversible; prefer /workspace for own work"),
        }


def _with_resolved(resolver_attr: str, action: str):
    """Decorator: resolve the target machine before calling the handler.

    Calls ``self.<resolver_attr>(params, action)``.  If the resolver
    returns an error dict, that dict is propagated as the handler's
    return value.  Otherwise the wrapped handler is invoked with
    ``(self, params, machine, backend)``.

    Centralises the "isinstance(resolved, dict)" gate that used to
    appear 12 times across docker_commit / stop / start / remove /
    inspect / logs / diff / stats / restart / ssh_disconnect /
    ssh_reconnect / ssh_remove.
    """

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, params, *args, **kwargs):
            resolver = getattr(self, resolver_attr)
            resolved = resolver(params, action)
            if isinstance(resolved, dict):
                return resolved
            machine, backend = resolved
            return fn(self, params, machine, backend, *args, **kwargs)

        return wrapper

    return deco


class SandboxEnv:
    """Dispatches sandbox_env actions and generates help responses."""

    def __init__(self, targets, shells, docker_backend, ssh_backend):
        self._machines = targets
        self._shells = shells
        self._docker = docker_backend
        self._ssh = ssh_backend

    def dispatch(self, action: str, params: dict) -> Any:
        handler = getattr(self, f"_op_{action}", None)
        if handler is None:
            return {
                "error": f"Unknown action: {action}. Call action=help for available operations."
            }
        try:
            return handler(params or {})
        except Exception as e:
            logger.exception("env.%s failed", action)
            return {"error": str(e), "type": type(e).__name__}

    # ---- discovery ----

    def _op_help(self, params):
        params = params or {}
        cfg = _load_config()
        # Build the canonical action index.  Order: HELP_RESPONSE (core)
        # then DOCKER_HELP_RESPONSE (always available — DockerBackend
        # is always imported), then SSH only when SSH is configured.
        all_ops: list[dict] = []
        all_ops.extend(HELP_RESPONSE["operations"])
        all_ops.extend(DOCKER_HELP_RESPONSE["operations"])
        if cfg.ssh.default_host:
            all_ops.extend(SSH_HELP_RESPONSE["operations"])

        topic = params.get("topic")
        if topic:
            for op in all_ops:
                if op.get("action") == topic:
                    return {"topic": topic, "operation": op}
            return {
                "error": (
                    f"Unknown action: {topic!r}. "
                    "Call action=help (no topic) to list available actions."
                ),
            }

        # Default: compact summary form (action + summary one-liner).
        # Saves ~70% tokens vs returning every long description; the
        # agent asks for per-action detail via topic=<action>.
        # To inspect multiple actions, call help(topic=X) per action.
        return {
            "default_actions": HELP_RESPONSE["default_actions"],
            "note": HELP_RESPONSE.get("note", ""),
            "operations": [
                {"action": op["action"], "summary": op.get("summary", "")}
                for op in all_ops
                if "action" in op
            ],
            "usage": 'help(topic="<action>") for full docs of one action.',
        }

    def _op_machine_list(self, params):
        """Lighter-weight alternative to status: machines only, no shells."""
        machines = []
        for name in self._machines.list_machines():
            info = self._machines.get_info(name)
            entry = {
                "name": name,
                "backend": info.backend,
                "status": info.status,
                "purpose": info.purpose or "",
                "shells": self._shells.count_shells(machine=name),
                "uptime": _format_uptime(self._machines.get_created_at(name)),
            }
            _inject_admin_info(name, entry)
            machines.append(entry)
        return {"machines": machines}

    def _op_status(self, params):
        machines = []
        for name in self._machines.list_machines():
            info = self._machines.get_info(name)
            entry = {
                "name": name,
                "backend": info.backend,
                "status": info.status,
                "purpose": info.purpose or "",
                "shells": self._shells.count_shells(machine=name),
                "uptime": _format_uptime(self._machines.get_created_at(name)),
            }
            _inject_admin_info(name, entry)
            machines.append(entry)
        return {
            "default_machine": self._machines.get_default(),
            "machines": machines,
            "shells": self._shells.list_shells(),
        }

    # ---- general ----

    def _require(self, params, *keys):
        missing = [k for k in keys if k not in params]
        if missing:
            return f"Missing required params: {', '.join(missing)}"
        return None

    def _resolve_machine_of_type(self, params, action, backend_cls, type_label):
        """Resolve a machine and assert its backend is of ``backend_cls``.

        Returns ``(machine, backend)`` on success or a ready-to-return
        ``{"error": ...}`` dict on any failure (missing key, unknown
        machine, wrong backend type).  Callers should use
        :func:`_with_resolved` to avoid the boilerplate isinstance check.
        """
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        if not isinstance(backend, backend_cls):
            return {"error": f"{action} only supported on {type_label} machines"}
        return machine, backend

    def _resolve_docker_machine(self, params, action: str) -> tuple[str, DockerBackend] | dict:
        """Thin wrapper kept for clarity at call sites — delegates to
        :meth:`_resolve_machine_of_type`.  Prefer the
        :func:`_with_resolved` decorator on the handler.
        """
        return self._resolve_machine_of_type(params, action, DockerBackend, "Docker")

    def _resolve_ssh_machine(self, params, action: str) -> tuple[str, SSHBackend] | dict:
        """SSH counterpart of :meth:`_resolve_docker_machine`."""
        return self._resolve_machine_of_type(params, action, SSHBackend, "SSH")

    def _op_default_set(self, params):
        has_machine = "machine" in params
        has_shell = "shell_id" in params
        if has_machine == has_shell:
            return {"error": "Pass exactly one of machine or shell_id"}
        if has_machine:
            machine = self._machines.resolve_machine(params["machine"])
            self._machines.set_default(machine)
            return {"default_machine": machine}
        shell_target = self._shells.get_machine(params["shell_id"])
        if shell_target is None:
            return {"error": f"Unknown shell_id: {params['shell_id']}"}
        self._shells.set_default(params["shell_id"])
        return {"default_shell": {"machine": shell_target, "shell_id": params["shell_id"]}}

    def _op_shell_new(self, params):
        machine = self._machines.resolve_machine(params.get("machine"))
        backend = self._machines.get_backend(machine)
        session = backend.open_shell(machine)
        shell_id = self._shells.open(machine, session, purpose=params.get("purpose", "manual"))
        # Surface the shell's live state so the agent can see whether
        # the new session is idle and ready, or already busy — without
        # having to call shell_list right after creation.
        result: dict[str, Any] = {
            "shell_id": shell_id,
            "machine": machine,
            "state": session.state,
        }
        if session.state in ("busy", "running"):
            result["bash_pid"] = session.bash_pid
        return result

    def _op_shell_remove(self, params):
        if "shell_id" not in params:
            return {"error": "Missing required param: shell_id"}
        if self._shells.close(params["shell_id"]):
            return {"shell_id": params["shell_id"], "status": "removed"}
        return {"error": f"Unknown shell_id: {params['shell_id']}"}

    def _op_shell_list(self, params):
        return self._shells.list_shells(machine=params.get("machine"))

    # ---- docker ----

    def _op_docker_run(self, params):
        err = self._require(params, "name", "image", "purpose")
        if err is not None:
            return {"error": err}
        kwargs = {"image": params["image"]}
        if "shell" in params:
            kwargs["shell"] = params["shell"]
        info = self._machines.register(
            params["name"],
            self._docker,
            purpose=params.get("purpose", ""),
            **kwargs,
        )
        # Surface status plus any diagnostic (error) and non-fatal hint
        # (note, e.g. "reattached to existing container").  Without this
        # the agent can't tell a fresh create from a 409 reattach, nor
        # see why a container failed to stay running.
        result = info.to_response(name_key="name")
        result["backend"] = "docker"
        if result.get("status") == "running":
            _inject_admin_info(params["name"], result)
            # Augment with a fresh curated inspect snapshot so the agent
            # sees image/id/cmd/mounts/state in the same response — saves
            # a separate docker_inspect round-trip immediately after
            # create.  Best-effort: inspect failure (e.g. race during a
            # very fast create-then-exit) is silently ignored; the
            # agent can still call docker_inspect directly.
            try:
                detail = self._docker.inspect(params["name"])
            except Exception:
                detail = None
            if isinstance(detail, dict) and "error" not in detail:
                for key in ("image", "id", "cmd", "mounts", "state"):
                    if key in detail:
                        result[key] = detail[key]
        return result

    def _op_docker_build(self, params):
        """Build a Docker image from a Dockerfile the agent has already
        written into a sandboxed container's ``/workspace/`` via
        :func:`sandbox_file_write`.

        ``dockerfile`` and ``context_dir`` must be CONTAINER paths under
        ``/workspace/``, NOT host paths — ``/workspace/`` is the only
        bind-mount from the host, so other container paths (e.g.
        ``/etc/foo``) live in the container's overlay FS and the daemon
        cannot read them.  Inline mode (``dockerfile_content``) is NOT
        supported — see the backend docstring for the security rationale.

        Returns ``error_kind`` to help the agent classify the failure:
        ``bad_path`` (non-/workspace path), ``context_invalid`` (context
        dir not found — probably wrote to a different machine),
        ``dockerfile_missing`` (the daemon couldn't open the Dockerfile),
        ``base_image_not_found`` (FROM <image> resolution failed),
        or ``build_failed`` (Dockerfile syntax, RUN error, etc.).
        """
        err = self._require(params, "image_tag")
        if err is not None:
            return {"error": err}
        machine = params.get("machine")
        if not machine:
            return {"error": "machine is required"}
        try:
            self._machines.resolve_machine(machine)
        except ValueError as e:
            return {"error": str(e)}
        return self._docker.build(
            params["image_tag"],
            machine=machine,
            dockerfile=params.get("dockerfile", "/workspace/Dockerfile"),
            context_dir=params.get("context_dir", "/workspace"),
        )

    def _op_docker_commit(self, params):
        """Commit a container's filesystem state to a new image.

        ``image_tag`` is required — every commit must produce a uniquely
        identifiable image to prevent silent overwrites (e.g. two
        concurrent ``dev`` machines both committing to the same default
        tag).
        """
        err = self._require(params, "image_tag")
        if err is not None:
            return {"error": err}
        resolved = self._resolve_docker_machine(params, "docker_commit")
        if isinstance(resolved, dict):
            return resolved
        machine, backend = resolved
        return backend.commit(machine, params["image_tag"])

    @_with_resolved("_resolve_docker_machine", "docker_stop")
    def _op_docker_stop(self, params, machine, backend):
        self._shells.close_all_for_machine(machine)
        return backend.stop(machine).to_response()

    @_with_resolved("_resolve_docker_machine", "docker_start")
    def _op_docker_start(self, params, machine, backend):
        return backend.start(machine).to_response()

    @_with_resolved("_resolve_docker_machine", "docker_remove")
    def _op_docker_remove(self, params, machine, backend):
        self._shells.close_all_for_machine(machine)
        result = backend.remove(machine)
        self._machines.unregister(machine)
        return result

    # ---- docker discovery (direct daemon queries) ----

    def _op_docker_ps(self, params):
        """List sandbox-mcp-managed containers.

        Queries the daemon for all containers with the
        ``sandbox-mcp.managed=true`` label, adopts each into
        :class:`TargetRegistry` (idempotent — ``adopt`` is a no-op if
        the machine is already known), and returns the result.

        This is both the list AND the refresh operation.  The server
        calls it once at startup (in ``SandboxServer.__init__``) to
        populate the registry.
        """
        managed = self._docker.list_managed_containers()
        containers = []
        for machine, attrs in managed:
            # Best-effort fields from container attrs.
            status = attrs.get("State", {}).get("Status", "unknown")
            running = status == "running"
            cfg = attrs.get("Config") or {}
            image = cfg.get("Image", "")
            # Purpose is persisted as a docker label (immutable after
            # creation); read it back so it survives restarts.
            labels = cfg.get("Labels") or {}
            purpose = labels.get("sandbox-mcp.purpose", "")
            info = TargetInfo(
                name=machine,
                backend="docker",
                status="running" if running else status,
                purpose=purpose,
                image=image,
                created=attrs.get("Created", ""),
            )
            self._machines.adopt(machine, self._docker, info)
            containers.append(
                {
                    "name": machine,
                    "status": info.status,
                    "image": info.image or "",
                    "purpose": info.purpose or "",
                    "created": info.created or "",
                }
            )
        # Newest first.
        containers.sort(key=lambda c: c["created"], reverse=True)
        return {"containers": containers}

    def _op_docker_images(self, params):
        return {"images": self._docker.list_images()}

    def _op_docker_image_history(self, params):
        err = self._require(params, "image")
        if err is not None:
            return {"error": err}
        return self._docker.history(params["image"])

    # ---- docker introspection ----

    def _op_docker_inspect(self, params):
        kind = params.get("kind", "container")
        if kind == "image":
            # Image mode doesn't use the per-machine resolver — the
            # agent passes the image ref directly, not a managed
            # machine name.  Kept inline rather than via
            # ``_with_resolved`` because the decorator would force
            # resolve_machine before we know we need to skip it.
            err = self._require(params, "machine")
            if err is not None:
                return {"error": err}
            return self._docker.inspect(
                params["machine"], kind="image", raw=bool(params.get("raw", False))
            )
        resolved = self._resolve_docker_machine(params, "docker_inspect")
        if isinstance(resolved, dict):
            return resolved
        machine, backend = resolved
        return backend.inspect(machine, raw=bool(params.get("raw", False)))

    @_with_resolved("_resolve_docker_machine", "docker_logs")
    def _op_docker_logs(self, params, machine, backend):
        return backend.logs(
            machine,
            tail=int(params.get("tail", 200)),
            since=params.get("since"),
            until=params.get("until"),
            timestamps=bool(params.get("timestamps", False)),
        )

    @_with_resolved("_resolve_docker_machine", "docker_diff")
    def _op_docker_diff(self, params, machine, backend):
        return backend.diff(machine)

    @_with_resolved("_resolve_docker_machine", "docker_stats")
    def _op_docker_stats(self, params, machine, backend):
        return backend.stats(machine)

    @_with_resolved("_resolve_docker_machine", "docker_restart")
    def _op_docker_restart(self, params, machine, backend):
        return backend.restart(machine, timeout=int(params.get("timeout", 10))).to_response()

    # ---- ssh ----

    def _op_ssh_connect(self, params):
        err = self._require(params, "name", "host", "user", "purpose")
        if err is not None:
            return {"error": err}
        info = self._machines.register(
            params["name"],
            self._ssh,
            purpose=params.get("purpose", ""),
            host=params["host"],
            user=params["user"],
            port=params.get("port", 22),
            key=params.get("key"),
            shell=params.get("shell", "bash"),
        )
        return {"name": info.name, "status": info.status, "backend": "ssh"}

    @_with_resolved("_resolve_ssh_machine", "ssh_disconnect")
    def _op_ssh_disconnect(self, params, machine, backend):
        self._shells.close_all_for_machine(machine)
        return backend.stop(machine).to_response()

    @_with_resolved("_resolve_ssh_machine", "ssh_reconnect")
    def _op_ssh_reconnect(self, params, machine, backend):
        return backend.start(machine).to_response()

    @_with_resolved("_resolve_ssh_machine", "ssh_remove")
    def _op_ssh_remove(self, params, machine, backend):
        self._shells.close_all_for_machine(machine)
        result = backend.remove(machine)
        self._machines.unregister(machine)
        return result
