"""Target registry: name -> backend + metadata + default target tracking."""

from __future__ import annotations

import time

from backends.base import Backend, TargetInfo


class TargetRegistry:
    """Tracks named execution targets and the default target."""

    def __init__(self):
        self._targets: dict[str, dict] = {}
        self._default: str | None = None

    def register(self, name: str, backend: Backend, purpose: str = "",
                 **kwargs) -> TargetInfo:
        info = backend.create(name, purpose=purpose, **kwargs)
        self._targets[name] = {
            "backend": backend,
            "info": info,
            "created_at": time.time(),
            "kwargs": kwargs,
            "purpose": purpose,
        }
        if self._default is None:
            self._default = name
        return info

    def unregister(self, name: str) -> bool:
        if name not in self._targets:
            return False
        del self._targets[name]
        if self._default == name:
            self._default = next(iter(self._targets), None)
        return True

    def list_targets(self) -> list[str]:
        return list(self._targets.keys())

    def get_info(self, name: str) -> TargetInfo:
        return self._targets[name]["info"]

    def get_backend(self, name: str) -> Backend:
        return self._targets[name]["backend"]

    def set_default(self, name: str) -> None:
        if name not in self._targets:
            raise ValueError(f"Unknown target: {name}")
        self._default = name

    def get_default(self) -> str | None:
        return self._default

    def resolve_target(self, name: str | None) -> str:
        """Return the resolved target name.

        - If name is None, return the default target.
        - If name is provided, return it unchanged (assumes it exists).
        """
        if name is None:
            if self._default is None:
                raise ValueError("No default target set")
            return self._default
        if name not in self._targets:
            raise ValueError(f"Unknown target: {name}")
        return name
