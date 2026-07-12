from unittest.mock import MagicMock, patch

import pytest

from sandbox_mcp.backends.docker_backend import DockerBackend


@pytest.fixture
def mock_client():
    """Return a MagicMock that stands in for ``docker.from_env()``.

    The returned object's ``containers`` attribute is itself a MagicMock
    whose ``run`` and ``get`` are configured to return sensible defaults.
    """
    client = MagicMock()
    client.containers = MagicMock()
    # containers.run returns a Mock container.
    mock_container = MagicMock()
    mock_container.short_id = "abc123"
    mock_container.attrs = {"State": {"Status": "running"}}
    # Default: containers.run succeeds.
    client.containers.run.return_value = mock_container
    client.containers.get.return_value = mock_container
    return client


@pytest.fixture
def docker_backend(mock_client):
    with patch("docker.from_env", return_value=mock_client):
        yield DockerBackend()


def test_docker_create(docker_backend, mock_client):
    info = docker_backend.create(
        name="dev",
        purpose="test",
        image="python:3.12",
        volumes=["/host:/container"],
        ports=["8080:8080"],
    )
    assert info.name == "dev"
    assert info.backend == "docker"
    assert info.status == "running"
    # Verify the SDK was called correctly.
    run_args = mock_client.containers.run.call_args
    assert run_args.args[0] == "python:3.12"
    run_kwargs = run_args.kwargs
    assert run_kwargs["name"] == "sandbox-dev"


def test_docker_create_uses_config_prefix(monkeypatch, tmp_path, mock_client):
    """Custom ``container_name_prefix`` from config flows into the SDK call."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[docker]\ncontainer_name_prefix = "box-"\n')
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg))
    monkeypatch.delenv("SANDBOX_MCP_DOCKER_CONTAINER_NAME_PREFIX", raising=False)
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path / "wh"))

    with patch("docker.from_env", return_value=mock_client):
        backend = DockerBackend()
        info = backend.create(name="dev", purpose="t", image="alpine:3")
    assert info.status == "running", f"error: {info.error!r}"
    run_kwargs = mock_client.containers.run.call_args.kwargs
    assert run_kwargs["name"] == "box-dev"


def test_docker_create_env_var_overrides_config_prefix(monkeypatch, tmp_path, mock_client):
    """Env var beats config file for prefix."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[docker]\ncontainer_name_prefix = "box-"\n')
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg))
    monkeypatch.setenv("SANDBOX_MCP_DOCKER_CONTAINER_NAME_PREFIX", "k8s-")
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path / "wh"))
    with patch("docker.from_env", return_value=mock_client):
        backend = DockerBackend()
        info = backend.create(name="dev", purpose="t", image="alpine:3")
    assert info.status == "running", f"error: {info.error!r}"
    run_kwargs = mock_client.containers.run.call_args.kwargs
    assert run_kwargs["name"] == "k8s-dev"


def test_docker_create_image_not_found(docker_backend, mock_client):
    from docker.errors import ImageNotFound

    mock_client.containers.run.side_effect = ImageNotFound("nope")
    info = docker_backend.create(name="dev", purpose="test", image="nonexistent:latest")
    assert info.status == "error"


def test_docker_stop(docker_backend, mock_client):
    container = mock_client.containers.get.return_value
    info = docker_backend.stop("dev")
    container.stop.assert_called_once_with(timeout=10)
    assert info.status == "stopped"


def test_docker_start(docker_backend, mock_client):
    container = mock_client.containers.get.return_value
    info = docker_backend.start("dev")
    container.start.assert_called_once()
    assert info.status == "running"


def test_docker_remove(docker_backend, mock_client):
    container = mock_client.containers.get.return_value
    result = docker_backend.remove("dev")
    container.remove.assert_called_once_with(force=True)
    assert result["status"] == "removed"


def test_docker_commit(docker_backend, mock_client):
    container = mock_client.containers.get.return_value
    result = docker_backend.commit("dev", "my-image:latest")
    container.commit.assert_called_once()
    assert result["status"] == "committed"


def test_docker_build(docker_backend, tmp_path):
    """build() needs a real Dockerfile on disk."""
    df = tmp_path / "Dockerfile"
    df.write_text("FROM python:3.12\n")
    with patch.object(docker_backend._ensure_client().images, "build") as mock_build:
        mock_build.return_value = (MagicMock(), [])
        result = docker_backend.build("my-image:latest", str(df), context_dir=str(tmp_path))
        assert result["status"] == "built"


def test_docker_open_shell(docker_backend, mock_client):
    """open_shell creates a ShellSession backed by DockerExecProcess."""
    container = mock_client.containers.get.return_value
    container.id = "c123"
    api = mock_client.api
    api.exec_create.return_value = {"Id": "e789"}

    # Create a pipe so the DockerExecProcess has real fds.
    import os

    r_out, w_out = os.pipe()
    r_in, w_in = os.pipe()

    # The sock._sock needs to be a real socket-like for sendall/recv.
    # Use a real socket for the mock.
    import socket

    a, b = socket.socketpair()

    socket_mock = MagicMock()
    socket_mock._sock = b
    api.exec_start.return_value = socket_mock

    shell = docker_backend.open_shell("dev")
    assert shell._external is True
    assert shell._process is not None
    assert hasattr(shell._process, "stdin")
    assert hasattr(shell._process, "stdout")
    shell.close()
    os.close(r_out)
    os.close(w_out)
    os.close(r_in)
    os.close(w_in)
    a.close()
    b.close()


def test_docker_exec_oneoff(docker_backend, mock_client):
    """exec_oneoff uses SDK exec_run (stdin data embedded in command)."""
    container = mock_client.containers.get.return_value
    container.exec_run.return_value = (0, b"hello\n")
    result = docker_backend.exec_oneoff("dev", "echo hello")
    assert result["exit_code"] == 0
    assert "hello" in result["output"]


def test_docker_list_containers_with_prefix(docker_backend, mock_client):
    mock_client.containers.list.return_value = []
    result = docker_backend.list_containers(name_prefix="sandbox-")
    assert result == []


def test_docker_list_containers(docker_backend, mock_client):
    """list_containers queries daemon directly, not MachineRegistry."""
    mock_container = MagicMock()
    mock_container.name = "sandbox-dev"
    mock_container.image = MagicMock()
    mock_container.image.tags = ["python:3.12"]
    mock_container.image.short_id = "sha256:abc"
    mock_container.attrs = {"State": {"Status": "running"}, "Created": "2025-01-01T00:00:00Z"}
    mock_client.containers.list.return_value = [mock_container]
    result = docker_backend.list_containers(name_prefix="sandbox-")
    assert len(result) == 1
    assert result[0]["name"] == "sandbox-dev"
    assert result[0]["status"] == "running"


def test_docker_list_images(docker_backend, mock_client):
    mock_client.images.list.return_value = []
    assert docker_backend.list_images() == []
