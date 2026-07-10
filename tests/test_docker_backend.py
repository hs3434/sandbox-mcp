from unittest.mock import MagicMock, patch

import pytest

from sandbox_mcp.backends.docker_backend import DockerBackend


@pytest.fixture
def docker_backend():
    return DockerBackend()


def test_docker_create(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="abc123\n")
        info = docker_backend.create(
            name="dev", purpose="test", image="python:3.12",
            volumes=["/host:/container"], ports=["8080:8080"],
        )
        assert info.name == "dev"
        assert info.backend == "docker"
        assert info.status == "running"
        call_args = mock_run.call_args[0][0]
        assert "run" in call_args
        assert "sandbox-dev" in call_args
        assert "python:3.12" in call_args


def test_docker_stop(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        info = docker_backend.stop("dev")
        assert info.status == "stopped"
        call_args = mock_run.call_args[0][0]
        assert "stop" in call_args
        assert "sandbox-dev" in call_args


def test_docker_start(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        info = docker_backend.start("dev")
        assert info.status == "running"
        call_args = mock_run.call_args[0][0]
        assert "start" in call_args
        assert "sandbox-dev" in call_args


def test_docker_remove(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = docker_backend.remove("dev")
        assert result["status"] == "removed"
        call_args = mock_run.call_args[0][0]
        assert "rm" in call_args
        assert "-f" in call_args
        assert "sandbox-dev" in call_args


def test_docker_commit(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = docker_backend.commit("dev", "my-image:latest")
        assert result["status"] == "committed"
        call_args = mock_run.call_args[0][0]
        assert "commit" in call_args
        assert "sandbox-dev" in call_args
        assert "my-image:latest" in call_args


def test_docker_build(tmp_path, docker_backend):
    df = tmp_path / "Dockerfile"
    df.write_text("FROM python:3.12\n")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = docker_backend.build("my-image:latest", str(df),
                                     context_dir=str(tmp_path))
        assert result["status"] == "built"
        call_args = mock_run.call_args[0][0]
        assert "build" in call_args
        assert "-t" in call_args
        assert "my-image:latest" in call_args


def test_docker_open_shell(docker_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        shell = docker_backend.open_shell("dev")
        assert "docker" in shell._args[0]
        assert "exec" in shell._args
        assert "sandbox-dev" in shell._args
        shell.close()
