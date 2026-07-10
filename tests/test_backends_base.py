import pytest

from backends.base import Backend, TargetInfo


def test_target_info_dataclass():
    info = TargetInfo(name="dev", backend="docker", status="running", purpose="Dev")
    assert info.name == "dev"
    assert info.backend == "docker"
    assert info.status == "running"
    assert info.purpose == "Dev"


def test_backend_is_abstract():
    with pytest.raises(TypeError):
        Backend()
