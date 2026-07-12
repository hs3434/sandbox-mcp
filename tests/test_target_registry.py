from unittest.mock import MagicMock

import pytest

from sandbox_mcp.target_registry import TargetRegistry


def test_register_machine():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(
        name="dev", backend="docker", status="running", purpose="test", shells=0, uptime=""
    )
    reg.register("dev", backend, purpose="test", image="python:3.12")
    assert "dev" in reg.list_machines()


def test_set_default_machine():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(
        name="dev", backend="docker", status="running", purpose="test", shells=0, uptime=""
    )
    reg.register("dev", backend, purpose="test", image="python:3.12")
    reg.set_default("dev")
    assert reg.get_default() == "dev"


def test_resolve_machine_explicit():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(
        name="dev", backend="docker", status="running", purpose="", shells=0, uptime=""
    )
    reg.register("dev", backend, purpose="", image="python:3.12")
    reg.register("db", backend, purpose="", image="postgres:16")
    reg.set_default("dev")
    assert reg.resolve_machine("db") == "db"
    assert reg.get_default() == "dev"


def test_resolve_machine_default():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(
        name="dev", backend="docker", status="running", purpose="", shells=0, uptime=""
    )
    reg.register("dev", backend, purpose="", image="python:3.12")
    reg.set_default("dev")
    assert reg.resolve_machine(None) == "dev"


def test_resolve_machine_no_default():
    reg = TargetRegistry()
    with pytest.raises(ValueError, match="No default machine"):
        reg.resolve_machine(None)


def test_unregister_machine():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(
        name="dev", backend="docker", status="running", purpose="", shells=0, uptime=""
    )
    reg.register("dev", backend, purpose="", image="python:3.12")
    reg.set_default("dev")
    reg.unregister("dev")
    assert "dev" not in reg.list_machines()
    assert reg.get_default() is None


def test_resolve_target_alias_still_works():
    """Legacy alias kept for backward compatibility."""
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(
        name="dev", backend="docker", status="running", purpose="", shells=0, uptime=""
    )
    reg.register("dev", backend, purpose="", image="python:3.12")
    assert reg.resolve_target(None) == "dev"
    assert reg.list_targets() == ["dev"]


def test_adopt_does_not_call_backend_create():
    """``adopt`` is for reconciling containers that already exist on the
    daemon (e.g. after server restart).  It must NOT call
    ``backend.create()`` — that would attempt to spin up a duplicate
    container and conflict with the existing one.
    """
    reg = TargetRegistry()
    backend = MagicMock()
    info = MagicMock(name="dev", backend="docker", status="running", purpose="reconciled")
    reg.adopt("dev", backend, info)
    assert "dev" in reg.list_machines()
    backend.create.assert_not_called()
    # Resolvable + retrievable.
    assert reg.resolve_machine("dev") == "dev"
    assert reg.get_backend("dev") is backend


def test_adopt_does_not_overwrite_existing_entry():
    """If a machine is already registered (e.g. mid-session), adopt must
    not clobber its state.  Reconciliation is idempotent."""
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(
        name="dev", backend="docker", status="running", purpose="created", shells=0, uptime=""
    )
    reg.register("dev", backend, purpose="created", image="python:3.12")
    new_info = MagicMock(name="dev", backend="docker", status="stopped", purpose="ignored")
    reg.adopt("dev", backend, new_info)  # must be a no-op
    # Original "created" purpose survives.
    assert reg.get_info("dev").purpose == "created"
