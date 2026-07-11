"""sandbox_env: progressive-discovery environment management.

Default actions advertised via tools/list: help, status.
Discovered via help: machine_list, default_set, shell_new/list/remove.
Discovered via docker_help/ssh_help: backend-specific lifecycle.
"""

from __future__ import annotations

import time
from typing import Any

HELP_RESPONSE = {
    "default_actions": [
        {"action": "help",
         "description": "Discover common management actions and backend help entries."},
        {"action": "status",
         "description": "Show current state: default machine, machine list, shell list."},
    ],
    "operations": [
        {
            "action": "machine_list",
            "description": ("List all registered machines with backend, status, "
                            "purpose, shell count, and uptime. Lighter than status "
                            "(no shell details)."),
            "example": {},
        },
        {
            "action": "default_set",
            "description": ("Set default machine or default shell. Pass machine to set "
                            "the default machine. Pass shell_id to set that shell as "
                            "its machine's default shell."),
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
            "description": ("Terminate and remove a shell session. If already "
                            "terminated, remove the registry entry."),
            "required": {"shell_id": "string"},
        },
        {
            "action": "shell_list",
            "description": "List all shells, optionally filtered by machine.",
            "optional": {"machine": "string"},
        },
    ],
    "more_help": {
        "docker_help": "Discover Docker machine actions: run/build/commit/stop/start/remove",
        "ssh_help": "Discover SSH machine actions: connect/disconnect/reconnect/remove",
    },
    "note": ("Core tools are directly exposed as sandbox_shell_exec, "
             "sandbox_shell_read, and sandbox_file_read/write/patch/search. "
             "Machine-aware tools support optional machine."),
}


DOCKER_HELP_RESPONSE = {
    "operations": [
        {
            "action": "docker_run",
            "description": "Create and start a Docker container.",
            "required": {"name": "string", "image": "string", "purpose": "string"},
            "optional": {
                "volumes": "string[] - e.g. ['/host:/container']",
                "ports": "string[] - e.g. ['8080:8080']",
                "env": "object",
                "workdir": "string - default /workspace",
            },
            "returns": {"name": "string", "status": "running", "backend": "docker"},
            "example": {"name": "dev", "image": "python:3.12", "purpose": "Python dev"},
        },
        {
            "action": "docker_build",
            "description": "Build a custom Docker image from a Dockerfile.",
            "required": {"image_tag": "string", "dockerfile": "string"},
            "optional": {"context_dir": "string"},
            "returns": {"image_tag": "string", "status": "built"},
        },
        {
            "action": "docker_commit",
            "description": "Save container state as a new image.",
            "required": {"machine": "string"},
            "optional": {"image_tag": "string - auto-generated if omitted"},
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
            "required": {"name": "string", "host": "string", "user": "string",
                         "purpose": "string"},
            "optional": {
                "port": "int - default 22",
                "key": "string - private key path (key auth only)",
            },
            "returns": {"name": "string", "status": "connected", "backend": "ssh"},
            "example": {"name": "remote", "host": "192.168.1.100",
                        "user": "ubuntu", "purpose": "Remote server"},
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
            return {"error": f"Unknown action: {action}. "
                              "Call action=help for available operations."}
        try:
            return handler(params or {})
        except Exception as e:
            return {"error": str(e)}

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
            machines.append({
                "name": name,
                "backend": info.backend,
                "status": info.status,
                "purpose": info.purpose or "",
                "shells": len(self._shells.list_shells(machine=name)),
                "uptime": _format_uptime(self._machines.get_created_at(name)),
            })
        return {"machines": machines}

    def _op_status(self, params):
        default = self._machines.get_default()
        machines = self._op_machine_list({})["machines"]
        return {"default_machine": default, "machines": machines,
                "shells": self._shells.list_shells()}

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
        return {"default_shell": {"machine": shell_target,
                                  "shell_id": params["shell_id"]}}

    def _op_shell_new(self, params):
        machine = self._machines.resolve_machine(params.get("machine"))
        backend = self._machines.get_backend(machine)
        session = backend.open_shell(machine)
        shell_id = self._shells.open(machine, session,
                                     purpose=params.get("purpose", "manual"))
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
            params["name"], self._docker,
            purpose=params.get("purpose", ""),
            image=params["image"],
            volumes=params.get("volumes", []),
            ports=params.get("ports", []),
            env=params.get("env", {}),
            workdir=params.get("workdir", "/workspace"),
        )
        return {"name": info.name, "status": info.status, "backend": "docker"}

    def _op_docker_build(self, params):
        err = self._require(params, "image_tag", "dockerfile")
        if err is not None:
            return {"error": err}
        return self._docker.build(params["image_tag"], params["dockerfile"],
                                  params.get("context_dir"))

    def _op_docker_commit(self, params):
        err = self._require(params, "machine")
        if err is not None:
            return {"error": err}
        machine = self._machines.resolve_machine(params["machine"])
        backend = self._machines.get_backend(machine)
        from sandbox_mcp.backends.docker_backend import DockerBackend
        if not isinstance(backend, DockerBackend):
            return {"error": "docker_commit only supported on Docker machines"}
        return backend.commit(machine, params.get("image_tag"))

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

    # ---- ssh ----

    def _op_ssh_connect(self, params):
        err = self._require(params, "name", "host", "user", "purpose")
        if err is not None:
            return {"error": err}
        info = self._machines.register(
            params["name"], self._ssh,
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
