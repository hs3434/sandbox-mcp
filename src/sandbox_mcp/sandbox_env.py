"""sandbox_env: progressive-discovery environment management.

Default actions advertised via tools/list: help, status.
Discovered via help: machine_list, default_set, shell_new/list/remove.
Discovered via docker_help/ssh_help: backend-specific lifecycle.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

HELP_RESPONSE = {
    "default_actions": [
        {
            "action": "help",
            "description": "Discover common management actions and backend help entries.",
        },
        {
            "action": "status",
            "description": "Show current state: default machine, machine list, shell list.",
        },
    ],
    "operations": [
        {
            "action": "machine_list",
            "description": (
                "List all registered machines with backend, status, "
                "purpose, shell count, and uptime. Lighter than status "
                "(no shell details)."
            ),
            "example": {},
        },
        {
            "action": "default_set",
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
            "description": "Create an additional shell session on a machine.",
            "optional": {"machine": "string", "purpose": "string"},
        },
        {
            "action": "shell_remove",
            "description": (
                "Terminate and remove a shell session. If already "
                "terminated, remove the registry entry."
            ),
            "required": {"shell_id": "string"},
        },
        {
            "action": "shell_list",
            "description": "List all shells, optionally filtered by machine.",
            "optional": {"machine": "string"},
        },
    ],
    "more_help": {
        "docker_help": (
            "Discover Docker machine actions: run/build/commit/stop/start/remove/ps/images"
        ),
        "ssh_help": "Discover SSH machine actions: connect/disconnect/reconnect/remove",
    },
    "note": (
        "Core tools are directly exposed as sandbox_shell_exec, "
        "sandbox_shell_read, and sandbox_file_read/write/patch/search. "
        "Machine-aware tools support optional machine."
    ),
}


DOCKER_HELP_RESPONSE = {
    "operations": [
        {
            "action": "docker_run",
            "description": (
                "Create and start a Docker container. Idempotent: "
                "if a container named sandbox-<name> already exists "
                "(e.g. after an MCP restart), the call attaches to "
                "it instead of creating a new one. An automatic workspace "
                "directory is created on the host and mounted to /workspace."
            ),
            "required": {"name": "string", "image": "string", "purpose": "string"},
            "optional": {
                "ports": "string[] - e.g. ['8080:8080']",
                "env": "object",
                "workdir": "string - default /workspace",
            },
            "returns": {"name": "string", "status": "running", "backend": "docker"},
            "example": {"name": "dev", "image": "python:3.12", "purpose": "Python dev"},
        },
        {
            "action": "docker_ps",
            "description": (
                "List existing Docker containers matching "
                "sandbox-* (direct daemon query, works even "
                "after restart when MachineRegistry is empty)."
            ),
            "optional": {"name_prefix": "string - filter by name prefix"},
            "returns": [
                {"name": "string", "status": "string", "image": "string", "created": "string"}
            ],
        },
        {
            "action": "docker_images",
            "description": "List available Docker images (direct daemon query).",
            "returns": [
                {"tag": "string", "image_id": "string", "created": "string", "size_mb": "number"}
            ],
        },
        {
            "action": "docker_build",
            "description": (
                "Build a Docker image from a Dockerfile already written "
                "into a sandboxed container's /workspace/ via "
                "sandbox_file_write.  Provide machine (default "
                "dockerfile=/workspace/Dockerfile, context_dir=/workspace); "
                "sandbox-mcp maps those container paths to work_home/<machine>/ "
                "on the host.  Inline dockerfile_content is not supported "
                "(see docker_backend.build docstring for rationale)."
            ),
            "required": {"image_tag": "string", "machine": "string"},
            "optional": {
                "dockerfile": "string — default /workspace/Dockerfile",
                "context_dir": "string — default /workspace",
            },
            "returns": {"image_tag": "string", "status": "built"},
        },
        {
            "action": "docker_commit",
            "description": (
                "Save container state as a new image.  image_tag is REQUIRED "
                "(format 'repo:tag', e.g. 'myapp:v1') — sandbox-mcp does not "
                "auto-generate one, to prevent silent overwrites when multiple "
                "machines commit."
            ),
            "required": {"machine": "string", "image_tag": "string"},
            "returns": {"image_tag": "string", "status": "committed"},
        },
        {
            "action": "docker_stop",
            "description": ("Stop container. State preserved, can docker_start to resume."),
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "stopped"},
        },
        {
            "action": "docker_start",
            "description": "Start a stopped container.",
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "running"},
        },
        {
            "action": "docker_remove",
            "description": ("Stop and remove container. Closes all shells for the machine."),
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "removed"},
        },
    ]
}


SSH_HELP_RESPONSE = {
    "operations": [
        {
            "action": "ssh_connect",
            "description": "Connect to an SSH remote machine (key auth only).",
            "required": {"name": "string", "host": "string", "user": "string", "purpose": "string"},
            "optional": {
                "port": "int - default 22",
                "key": "string - private key path (key auth only)",
            },
            "returns": {"name": "string", "status": "connected", "backend": "ssh"},
            "example": {
                "name": "remote",
                "host": "192.168.1.100",
                "user": "ubuntu",
                "purpose": "Remote server",
            },
        },
        {
            "action": "ssh_disconnect",
            "description": "Close SSH connection. Remote machine is not affected.",
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "stopped"},
        },
        {
            "action": "ssh_reconnect",
            "description": "Re-establish SSH connection. Shells are lost on disconnect.",
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "running"},
        },
        {
            "action": "ssh_remove",
            "description": "Unregister SSH machine. Remote machine is not affected.",
            "required": {"machine": "string"},
            "returns": {"machine": "string", "status": "removed"},
        },
    ]
}


def _format_uptime(created_at: float) -> str:
    uptime_s = int(time.time() - created_at)
    if uptime_s > 60:
        return f"{uptime_s // 3600}h{(uptime_s % 3600) // 60}m"
    return f"{uptime_s}s"


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
            logger.exception("sandbox_env.%s failed", action)
            return {"error": str(e), "type": type(e).__name__}

    # ---- discovery ----

    def _op_help(self, params):
        return HELP_RESPONSE

    def _op_docker_help(self, params):
        return DOCKER_HELP_RESPONSE

    def _op_ssh_help(self, params):
        return SSH_HELP_RESPONSE

    def _op_machine_list(self, params):
        """Lighter-weight alternative to status: machines only, no shells."""
        machines = []
        for name in self._machines.list_machines():
            info = self._machines.get_info(name)
            machines.append(
                {
                    "name": name,
                    "backend": info.backend,
                    "status": info.status,
                    "purpose": info.purpose or "",
                    "shells": len(self._shells.list_shells(machine=name)),
                    "uptime": _format_uptime(self._machines.get_created_at(name)),
                }
            )
        return {"machines": machines}

    def _op_status(self, params):
        default = self._machines.get_default()
        machines = self._op_machine_list({})["machines"]
        return {
            "default_machine": default,
            "machines": machines,
            "shells": self._shells.list_shells(),
        }

    # ---- general ----

    def _require(self, params, *keys):
        missing = [k for k in keys if k not in params]
        if missing:
            return f"Missing required params: {', '.join(missing)}"
        return None

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
        return {"shell_id": shell_id, "machine": machine}

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
        info = self._machines.register(
            params["name"],
            self._docker,
            purpose=params.get("purpose", ""),
            image=params["image"],
            ports=params.get("ports", []),
            env=params.get("env", {}),
            workdir=params.get("workdir", "/workspace"),
        )
        return {"name": info.name, "status": info.status, "backend": "docker"}

    def _op_docker_build(self, params):
        """Build a Docker image from a Dockerfile the agent has already
        written into a sandboxed container's ``/workspace/`` via
        :func:`sandbox_file_write`.

        ``dockerfile`` and ``context_dir`` must be under ``/workspace/``;
        host paths are rejected at the sandbox boundary.  Inline mode
        (``dockerfile_content``) is not supported — see the backend
        docstring for the security rationale.
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
        err = self._require(params, "machine", "image_tag")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend

        if not isinstance(backend, DockerBackend):
            return {"error": "docker_commit only supported on Docker machines"}
        return backend.commit(machine, params["image_tag"])

    def _op_docker_stop(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend

        if not isinstance(backend, DockerBackend):
            return {"error": "docker_stop only supported on Docker machines"}
        self._shells.close_all_for_machine(machine)
        info = backend.stop(machine)
        return {"machine": machine, "status": info.status}

    def _op_docker_start(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend

        if not isinstance(backend, DockerBackend):
            return {"error": "docker_start only supported on Docker machines"}
        info = backend.start(machine)
        return {"machine": machine, "status": info.status}

    def _op_docker_remove(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend

        if not isinstance(backend, DockerBackend):
            return {"error": "docker_remove only supported on Docker machines"}
        self._shells.close_all_for_machine(machine)
        result = backend.remove(machine)
        self._machines.unregister(machine)
        return result

    # ---- docker discovery (direct daemon queries) ----

    def _op_docker_ps(self, params):
        name_prefix = params.get("name_prefix", "sandbox-")
        return {"containers": self._docker.list_containers(name_prefix=name_prefix)}

    def _op_docker_images(self, params):
        return {"images": self._docker.list_images()}

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
        )
        return {"name": info.name, "status": info.status, "backend": "ssh"}

    def _op_ssh_disconnect(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.ssh_backend import SSHBackend

        if not isinstance(backend, SSHBackend):
            return {"error": "ssh_disconnect only supported on SSH machines"}
        self._shells.close_all_for_machine(machine)
        info = backend.stop(machine)
        return {"machine": machine, "status": info.status}

    def _op_ssh_reconnect(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.ssh_backend import SSHBackend

        if not isinstance(backend, SSHBackend):
            return {"error": "ssh_reconnect only supported on SSH machines"}
        info = backend.start(machine)
        return {"machine": machine, "status": info.status}

    def _op_ssh_remove(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.ssh_backend import SSHBackend

        if not isinstance(backend, SSHBackend):
            return {"error": "ssh_remove only supported on SSH machines"}
        self._shells.close_all_for_machine(machine)
        result = backend.remove(machine)
        self._machines.unregister(machine)
        return result
