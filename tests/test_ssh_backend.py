from unittest.mock import MagicMock, patch

import pytest

from sandbox_mcp.backends.ssh_backend import SSHBackend


@pytest.fixture
def ssh_backend():
    return SSHBackend()


def test_ssh_create(ssh_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        info = ssh_backend.create(
            name="remote", purpose="remote", host="192.168.1.100", user="ubuntu",
        )
        assert info.name == "remote"
        assert info.backend == "ssh"
        assert info.status == "running"


def test_ssh_stop_disconnects(ssh_backend):
    ssh_backend._targets["remote"] = {
        "host": "192.168.1.100", "user": "ubuntu", "port": 22,
        "socket": "/tmp/sandbox-mcp-ssh-remote",
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        info = ssh_backend.stop("remote")
        assert info.status == "stopped"


def test_ssh_remove_unregisters(ssh_backend):
    ssh_backend._targets["remote"] = {"host": "h", "user": "u", "port": 22}
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = ssh_backend.remove("remote")
        assert result["status"] == "removed"
        assert "remote" not in ssh_backend._targets


def test_ssh_open_shell(ssh_backend):
    ssh_backend._targets["remote"] = {
        "host": "192.168.1.100", "user": "ubuntu", "port": 22,
        "socket": "/tmp/sandbox-mcp-ssh-remote", "key": None,
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        shell = ssh_backend.open_shell("remote")
        assert "ssh" in shell._args[0]
        shell.close()
