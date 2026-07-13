# sandbox-mcp - Sandbox Environment Manager MCP server
# Copyright (C) 2024  Sandbox MCP Contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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
            name="remote",
            purpose="remote",
            host="192.168.1.100",
            user="ubuntu",
        )
        assert info.name == "remote"
        assert info.backend == "ssh"
        assert info.status == "running"


def test_ssh_stop_disconnects(ssh_backend):
    ssh_backend._targets["remote"] = {
        "host": "192.168.1.100",
        "user": "ubuntu",
        "port": 22,
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


def test_ssh_create_allocates_socket_dir_and_remove_reaps_it(ssh_backend, tmp_path):
    """``_socket_path`` uses ``tempfile.mkdtemp`` to host the SSH control
    socket; ``remove()`` must ``shutil.rmtree`` that dir, otherwise a
    long-running server leaks one directory per SSH target.
    """
    with patch("sandbox_mcp.backends.ssh_backend.tempfile.mkdtemp") as mock_mkdtemp:
        mock_mkdtemp.return_value = str(tmp_path / "sandbox-mcp-ssh-remote-abc")
        (tmp_path / "sandbox-mcp-ssh-remote-abc").mkdir()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            ssh_backend.create(
                name="remote",
                purpose="remote",
                host="192.168.1.100",
                user="ubuntu",
            )
            assert (tmp_path / "sandbox-mcp-ssh-remote-abc").is_dir()
            ssh_backend.remove("remote")
        assert not (tmp_path / "sandbox-mcp-ssh-remote-abc").exists(), (
            "remove() must rmtree the socket dir created by mkdtemp"
        )


def test_ssh_open_shell(ssh_backend):
    ssh_backend._targets["remote"] = {
        "host": "192.168.1.100",
        "user": "ubuntu",
        "port": 22,
        "socket": "/tmp/sandbox-mcp-ssh-remote",
        "key": None,
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        shell = ssh_backend.open_shell("remote")
        assert "ssh" in shell._args[0]
        shell.close()


def test_ssh_write_file_streams_content_via_stdin(ssh_backend):
    """write_file pipes content over SSH stdin (no shell ARG_MAX)."""
    ssh_backend._targets["remote"] = {
        "host": "h",
        "user": "u",
        "port": 22,
        "key": None,
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = ssh_backend.write_file("remote", "/tmp/x.txt", b"hello world\n")
    assert result["status"] == "ok"
    assert result["bytes_written"] == 12
    # Verify subprocess.run was called with content as stdin
    call = mock_run.call_args
    assert call.kwargs["input"] == b"hello world\n"
    # The command should set -e + mktemp + cat > + mv
    cmd = call.args[0][-1]  # last positional arg is the bash -c command
    assert "set -e" in cmd
    assert "cat >" in cmd
    assert "mv -f" in cmd


def test_ssh_write_file_propagates_error(ssh_backend):
    ssh_backend._targets["remote"] = {
        "host": "h",
        "user": "u",
        "port": 22,
        "key": None,
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="permission denied",
        )
        result = ssh_backend.write_file("remote", "/tmp/x.txt", b"hi")
    assert result["status"] == "error"
    assert "permission denied" in result["error"]
