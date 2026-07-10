from unittest.mock import MagicMock

import pytest

from sandbox_mcp.target_registry import TargetRegistry


def test_register_target():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(name="dev", backend="docker",
                                             status="running", purpose="test",
                                             shells=0, uptime="")
    reg.register("dev", backend, purpose="test", image="python:3.12")
    assert "dev" in reg.list_targets()


def test_set_default_target():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(name="dev", backend="docker",
                                             status="running", purpose="test",
                                             shells=0, uptime="")
    reg.register("dev", backend, purpose="test", image="python:3.12")
    reg.set_default("dev")
    assert reg.get_default() == "dev"


def test_resolve_target_explicit():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(name="dev", backend="docker",
                                             status="running", purpose="",
                                             shells=0, uptime="")
    reg.register("dev", backend, purpose="", image="python:3.12")
    reg.register("db", backend, purpose="", image="postgres:16")
    reg.set_default("dev")
    assert reg.resolve_target("db") == "db"
    assert reg.get_default() == "dev"


def test_resolve_target_default():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(name="dev", backend="docker",
                                             status="running", purpose="",
                                             shells=0, uptime="")
    reg.register("dev", backend, purpose="", image="python:3.12")
    reg.set_default("dev")
    assert reg.resolve_target(None) == "dev"


def test_resolve_target_no_default():
    reg = TargetRegistry()
    with pytest.raises(ValueError, match="No default target"):
        reg.resolve_target(None)


def test_unregister_target():
    reg = TargetRegistry()
    backend = MagicMock()
    backend.create.return_value = MagicMock(name="dev", backend="docker",
                                             status="running", purpose="",
                                             shells=0, uptime="")
    reg.register("dev", backend, purpose="", image="python:3.12")
    reg.set_default("dev")
    reg.unregister("dev")
    assert "dev" not in reg.list_targets()
    assert reg.get_default() is None
