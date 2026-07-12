"""Machine registry: name -> backend + metadata + default machine tracking.

Public API uses the term "machine" because that's what the agent sees
in tool parameters and responses. The internal attribute `_machines`
replaces the earlier `_targets`.
"""

from __future__ import annotations

import time

from sandbox_mcp.backends.base import Backend, TargetInfo


class TargetRegistry:
    """Tracks named execution machines and the default machine.

    The registry is populated two ways:

    - ``register(name, backend, purpose)`` calls ``backend.create()`` and
      records the freshly-created machine.  Used by the
      ``sandbox_env`` dispatcher on agent-initiated ``docker_run``.
    - ``adopt(name, backend, info)`` records a pre-existing machine
      WITHOUT calling ``backend.create()``.  Used by the server's
      startup reconciliation pass to re-discover containers that
      survived a restart (identified by the
      ``sandbox-mcp.managed=true`` docker label).

    Adopting an already-registered name is a no-op — reconciliation
    must not clobber a machine's in-process state with stale daemon
    data.
    """

    def __init__(self):
        self._machines: dict[str, dict] = {}
        self._default: str | None = None

    def register(self, name: str, backend: Backend, purpose: str = "", **kwargs) -> TargetInfo:
        info = backend.create(name, purpose=purpose, **kwargs)
        self._machines[name] = {
            "backend": backend,
            "info": info,
            "created_at": time.time(),
            "kwargs": kwargs,
            "purpose": purpose,
        }
        if self._default is None:
            self._default = name
        return info

    def adopt(self, name: str, backend: Backend, info: TargetInfo) -> None:
        """Record ``name`` as a known machine without invoking
        ``backend.create()``.  Idempotent: no-op if already registered.

        Used by the server's startup reconciliation pass — the daemon
        is the source of truth for what exists; the registry is just a
        cached view that gets rebuilt at every server start.
        """
        if name in self._machines:
            return
        self._machines[name] = {
            "backend": backend,
            "info": info,
            "created_at": time.time(),
            "kwargs": {},
            "purpose": info.purpose,
        }
        if self._default is None:
            self._default = name

    def unregister(self, name: str) -> bool:
        if name not in self._machines:
            return False
        del self._machines[name]
        if self._default == name:
            self._default = next(iter(self._machines), None)
        return True

    def list_machines(self) -> list[str]:
        return list(self._machines.keys())

    # Backward-compatible alias; the rest of the codebase still calls
    # `list_targets()` and `resolve_target()` in a few places.
    list_targets = list_machines

    def get_info(self, name: str) -> TargetInfo:
        return self._machines[name]["info"]

    def get_backend(self, name: str) -> Backend:
        return self._machines[name]["backend"]

    def get_created_at(self, name: str) -> float:
        return self._machines[name].get("created_at", time.time())

    def set_default(self, name: str) -> None:
        if name not in self._machines:
            raise ValueError(f"Unknown machine: {name}")
        self._default = name

    def get_default(self) -> str | None:
        return self._default

    def resolve_machine(self, name: str | None) -> str:
        """Resolve a machine name to its canonical name.

        - If name is None, return the default machine.
        - If name is provided, validate it exists.
        """
        if name is None:
            if self._default is None:
                raise ValueError("No default machine set")
            return self._default
        if name not in self._machines:
            raise ValueError(f"Unknown machine: {name}")
        return name

    # Backward-compatible alias.
    resolve_target = resolve_machine
