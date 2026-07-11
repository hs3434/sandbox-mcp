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
        name="dev", purpose="test", image="python:3.12",
        volumes=["/host:/container"], ports=["8080:8080"],
    )
    assert info.name == "dev"
    assert info.backend == "docker"
    assert info.status == "running"
    # Verify the SDK was called correctly.
    run_args = mock_client.containers.run.call_args
    assert run_args.args[0] == "python:3.12"
    run_kwargs = run_args.kwargs
    assert run_kwargs["name"] == "sandbox-dev"


def test_docker_create_image_not_found(docker_backend, mock_client):
    from docker.errors import ImageNotFound
    mock_client.containers.run.side_effect = ImageNotFound("nope")
    info = docker_backend.create(
        name="dev", purpose="test", image="nonexistent:latest")
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
        result = docker_backend.build("my-image:latest", str(df),
                                      context_dir=str(tmp_path))
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


def test_docker_exec_oneoff_no_stdin(docker_backend, mock_client):
    """exec_oneoff without stdin uses high-level exec_run."""
    container = mock_client.containers.get.return_value
    container.exec_run.return_value = (0, b"hello\n")
    result = docker_backend.exec_oneoff("dev", "echo hello")
    assert result["exit_code"] == 0
    assert "hello" in result["output"]


def test_docker_exec_oneoff_with_stdin(docker_backend, mock_client):
    """exec_oneoff with stdin_data uses low-level socket API."""
    container = mock_client.containers.get.return_value
    container.id = "c789"
    api = mock_client.api
    api.exec_create.return_value = {"Id": "e123"}

    # Build a Docker multiplexed frame: stdout type + payload_len + payload
    import struct
    payload = b"hello via sdk exec\n"
    header_bytes = b"\x01\x00\x00\x00" + struct.pack(">I", len(payload))
    socket_mock = MagicMock()
    socket_mock._sock = MagicMock()
    # Return header first, then payload, then trigger EOF.
    socket_mock._sock.recv.side_effect = [
        header_bytes,             # first call: 8-byte header
        payload,                  # second call: payload
        b"",                      # third call: EOF (b"")
        OSError(),                # fourth call: any subsequent reads raise
    ]
    api.exec_start.return_value = socket_mock
    api.exec_inspect.return_value = {"ExitCode": 0}

    result = docker_backend.exec_oneoff("dev", "cat", stdin_data="hello via sdk exec\n")
    assert result["exit_code"] == 0
    assert "hello via sdk exec" in result["output"]
