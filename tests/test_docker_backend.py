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

import socket
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import docker
from sandbox_mcp.backends.docker_backend import DockerBackend


@pytest.fixture(autouse=True)
def _redirect_work_home(tmp_path, monkeypatch):
    """Auto-redirect ``[storage] work_home`` to a per-test tmp dir.

    docker_backend.create() now auto-creates ``work_home/_share/<name>/``
    for the inter-container share, so every create() call writes to the
    filesystem.  Without this fixture, tests would pollute the real
    ``~/.sandbox-mcp/workspaces/_share/`` and leak between runs.  Tests
    that need a custom work_home path can still override via their own
    ``monkeypatch.setenv`` (later calls win).
    """
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))


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


def test_docker_create(docker_backend, mock_client, tmp_path, monkeypatch):
    """End-to-end create() with no special params.

    Three auto mounts: ``work_home/dev`` → ``/workspace`` (workspace),
    ``work_home/_share`` → ``/share`` (share root, ro), and
    ``work_home/_share/dev`` → ``/share/dev`` (self overlay,
    rw).  No agent-supplied host paths leak through (security boundary).
    """
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    info = docker_backend.create(
        name="dev",
        purpose="test",
        image="python:3.12",
    )
    assert info.name == "dev"
    assert info.backend == "docker"
    assert info.status == "running", f"unexpected error: {info.error!r}"
    run_args = mock_client.containers.run.call_args
    assert run_args.args[0] == "python:3.12"
    run_kwargs = run_args.kwargs
    assert run_kwargs["name"] == "dev"
    mounts = run_kwargs.get("volumes") or {}
    assert len(mounts) == 3, f"expected workspace + 2 share mounts, got: {mounts}"
    bind_targets = {m["bind"] for m in mounts.values()}
    assert bind_targets == {
        "/workspace",
        "/share",
        "/share/dev",
    }, bind_targets
    labels = run_kwargs.get("labels") or {}
    assert labels.get("sandbox-mcp.managed") == "true"
    assert labels.get("sandbox-mcp.machine") == "dev"


def test_docker_create_writes_purpose_label(docker_backend, mock_client, tmp_path, monkeypatch):
    """purpose is persisted as a docker label so it survives restarts
    (read back by docker_ps reconciliation)."""
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    docker_backend.create(name="dev", purpose="Python dev box", image="python:3.12")
    labels = mock_client.containers.run.call_args.kwargs.get("labels") or {}
    assert labels.get("sandbox-mcp.purpose") == "Python dev box"


def test_docker_create_omits_purpose_label_when_empty(
    docker_backend, mock_client, tmp_path, monkeypatch
):
    """Empty purpose -> no label key (absence == 'no purpose', cleaner
    than an empty-string value)."""
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    docker_backend.create(name="dev", purpose="", image="python:3.12")
    labels = mock_client.containers.run.call_args.kwargs.get("labels") or {}
    assert "sandbox-mcp.purpose" not in labels
    # The identity labels are still present.
    assert labels.get("sandbox-mcp.managed") == "true"
    assert labels.get("sandbox-mcp.machine") == "dev"


def test_docker_create_ignores_volumes_kwarg(docker_backend, mock_client, tmp_path, monkeypatch):
    """Agent cannot smuggle arbitrary host paths into the container via
    a Docker SDK ``volumes`` kwarg.  The auto-mounted bindings are
    workspace + share; attacker-supplied mounts are silently dropped.
    """
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    docker_backend.create(
        name="dev",
        purpose="test",
        image="python:3.12",
        volumes=["/etc:/host-etc", "/root:/host-root"],  # attacker attempt
    )
    mounts = mock_client.containers.run.call_args.kwargs.get("volumes") or {}
    # All mounts must end under work_home — never /etc, /root, etc.
    for host_path in mounts:
        assert "etc" not in str(host_path).split("/"), f"host /etc leaked: {host_path}"
        assert "root" not in str(host_path).split("/"), f"host /root leaked: {host_path}"


# ---- inter-container share dir -------------------------------------------


def test_docker_create_share_uses_two_mounts_not_per_peer(docker_backend, mock_client, tmp_path):
    """The share is set up as exactly two bind mounts (parent ro + self
    rw overlay), regardless of how many peer subdirs exist — peer count
    has zero impact on mount count or startup time.
    """
    (tmp_path / "_share" / "alice").mkdir(parents=True)
    (tmp_path / "_share" / "bob").mkdir(parents=True)
    (tmp_path / "_share" / "carol").mkdir(parents=True)

    docker_backend.create(name="dev", purpose="t", image="alpine:3")
    mounts = mock_client.containers.run.call_args.kwargs["volumes"]
    share_mounts = [m for m in mounts.values() if m["bind"].startswith("/share")]
    assert len(share_mounts) == 2, (
        f"expected parent ro + self overlay, got {len(share_mounts)} mounts: {share_mounts}"
    )
    parent = next(m for m in share_mounts if m["bind"] == "/share")
    assert parent["mode"] == "ro", parent
    overlay = next(m for m in share_mounts if m["bind"] == "/share/dev")
    assert overlay["mode"] == "rw", overlay


def test_docker_create_share_creates_root_and_self_on_first_use(
    docker_backend, mock_client, tmp_path
):
    """First create() also creates work_home/_share/ and work_home/_share/<self>/.

    Both are needed: the parent mount requires the root, the overlay
    requires the self subdir (otherwise the mount source is missing).
    """
    assert not (tmp_path / "_share").exists()
    docker_backend.create(name="dev", purpose="t", image="alpine:3")
    assert (tmp_path / "_share").is_dir()
    assert (tmp_path / "_share" / "dev").is_dir()


def test_docker_create_share_disabled_when_subdir_empty(docker_backend, mock_client, monkeypatch):
    """Setting `[storage] share_subdir = ""` disables the share mount
    entirely — only the per-machine workspace bind remains.
    """
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_SHARE_SUBDIR", "")
    docker_backend.create(name="dev", purpose="t", image="alpine:3")
    mounts = mock_client.containers.run.call_args.kwargs["volumes"]
    bind_targets = {m["bind"] for m in mounts.values()}
    assert "/workspace" in bind_targets
    assert not any(b.startswith("/share/") for b in bind_targets), bind_targets


def test_docker_create_share_sees_existing_peers_through_parent(
    docker_backend, mock_client, tmp_path
):
    """A peer subdir created before this container starts is reachable
    through the parent ro mount — no per-peer bind entry needed.  The
    mount's contents are evaluated by the kernel on access, so any
    peer subdir that exists on the host shows up at the corresponding
    path inside the container.
    """
    (tmp_path / "_share" / "alice").mkdir(parents=True)
    (tmp_path / "_share" / "alice" / "out.txt").write_text("hi")
    docker_backend.create(name="dev", purpose="t", image="alpine:3")
    mounts = mock_client.containers.run.call_args.kwargs["volumes"]
    bind_targets = {m["bind"] for m in mounts.values()}
    # alice/ is NOT an explicit bind — it surfaces through /share/.
    assert "/share/alice" not in bind_targets, bind_targets
    assert "/share" in bind_targets


# ---- admin machine --------------------------------------------------------


def test_docker_create_admin_uses_own_and_host_mounts(docker_backend, mock_client, tmp_path):
    """Admin container gets TWO workspace-style mounts: own scratch at
    ``/workspace`` (work_home/admin/) AND global view at ``/host``
    (work_home itself).  Share bindings are skipped because the global
    mount already covers ``work_home/_share/``.
    """
    info = docker_backend.create(name="admin", purpose="admin", image="alpine:3")
    assert info.status == "running", f"unexpected error: {info.error!r}"
    mounts = mock_client.containers.run.call_args.kwargs["volumes"]
    bind_targets = {m["bind"] for m in mounts.values()}
    assert bind_targets == {"/workspace", "/host"}, bind_targets
    # Both mounts must be rw.
    assert all(m["mode"] == "rw" for m in mounts.values()), mounts
    # No /share mount for admin (covered by /host).
    assert not any(b.startswith("/share") for b in bind_targets), bind_targets


def test_docker_create_admin_skips_share_dir_creation(docker_backend, mock_client, tmp_path):
    """Admin does NOT create ``work_home/_share/admin/`` — peers must
    not see admin as a share peer (admin is an ops channel, not a
    collaborator).
    """
    docker_backend.create(name="admin", purpose="admin", image="alpine:3")
    assert not (tmp_path / "_share" / "admin").exists(), (
        "_share/admin/ should not be auto-created for admin"
    )


def test_docker_create_admin_disabled_when_admin_machine_empty(
    docker_backend, mock_client, tmp_path, monkeypatch
):
    """When ``admin_machine = ""``, the name ``admin`` is a normal peer —
    share mount is added, no /host, default_image is used.
    """
    monkeypatch.setenv("SANDBOX_MCP_DOCKER_ADMIN_MACHINE", "")
    docker_backend.create(name="admin", purpose="ops", image="alpine:3")
    mounts = mock_client.containers.run.call_args.kwargs["volumes"]
    bind_targets = {m["bind"] for m in mounts.values()}
    # Peer-style mount layout: workspace + share parent + share overlay.
    assert bind_targets == {
        "/workspace",
        "/share",
        "/share/admin",
    }, bind_targets
    assert "/host" not in bind_targets, bind_targets


def test_docker_create_admin_explicit_image_kwarg_wins(
    docker_backend, mock_client, tmp_path, monkeypatch
):
    """Agent-supplied ``image`` kwarg beats ``default_image``
    (matches peer behaviour — explicit > config).
    """
    monkeypatch.setenv("SANDBOX_MCP_DOCKER_DEFAULT_IMAGE", "debian:stable-slim")
    docker_backend.create(name="admin", purpose="admin", image="busybox:latest")
    assert mock_client.containers.run.call_args.args[0] == "busybox:latest"


def test_docker_create_uses_bare_machine_name(monkeypatch, tmp_path, mock_client):
    """Container names are no longer prefixed — the label is the
    namespace marker.  The legacy ``container_name_prefix`` config key
    (if still present in user config) is silently ignored.
    """
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
    # Bare name; prefix is ignored.
    assert run_kwargs["name"] == "dev"


def test_ensure_client_uses_config_host_when_set(monkeypatch, tmp_path, mock_client):
    """[docker] host in config.toml replaces the $DOCKER_HOST / unix-socket default.

    Useful when sandbox-mcp runs in a container with the host socket
    bind-mounted at a non-default path, or when pointing at a remote
    docker daemon (TCP / TLS / ssh transport).
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text('[docker]\nhost = "tcp://10.0.5.20:2376"\n')
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg))
    monkeypatch.delenv("SANDBOX_MCP_DOCKER_HOST", raising=False)
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path / "wh"))

    with (
        patch("docker.from_env") as mock_from_env,
        patch("docker.DockerClient", return_value=mock_client) as mock_explicit,
    ):
        backend = DockerBackend()
        client = backend._ensure_client()
    assert client is mock_client
    # Explicit DockerClient(base_url=..., tls=..., cert=...) was used;
    # from_env() was NOT consulted.
    mock_explicit.assert_called_once_with(base_url="tcp://10.0.5.20:2376", tls=None, cert=None)
    mock_from_env.assert_not_called()


def test_ensure_client_uses_config_host_with_tls(monkeypatch, tmp_path, mock_client):
    """tls_verify=true + cert_path flow into DockerClient(tls=..., cert=...)."""
    cfg = tmp_path / "config.toml"
    certs = tmp_path / "certs"
    certs.mkdir()
    cfg.write_text(
        f'[docker]\nhost = "tcp://docker.example:2376"\ntls_verify = true\ncert_path = "{certs}"\n'
    )
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg))
    monkeypatch.delenv("SANDBOX_MCP_DOCKER_HOST", raising=False)
    monkeypatch.delenv("SANDBOX_MCP_DOCKER_TLS_VERIFY", raising=False)
    monkeypatch.delenv("SANDBOX_MCP_DOCKER_CERT_PATH", raising=False)
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path / "wh"))

    with patch("docker.DockerClient", return_value=mock_client) as mock_explicit:
        backend = DockerBackend()
        backend._ensure_client()
    mock_explicit.assert_called_once_with(
        base_url="tcp://docker.example:2376", tls=True, cert=str(certs)
    )


def test_ensure_client_falls_back_to_from_env_when_host_empty(monkeypatch, tmp_path, mock_client):
    """Empty ``[docker] host`` (the default) delegates to ``from_env()``."""
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.delenv("SANDBOX_MCP_DOCKER_HOST", raising=False)
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path / "wh"))

    with (
        patch("docker.from_env", return_value=mock_client) as mock_from_env,
        patch("docker.DockerClient") as mock_explicit,
    ):
        backend = DockerBackend()
        backend._ensure_client()
    mock_from_env.assert_called_once()
    mock_explicit.assert_not_called()


def test_docker_create_env_var_overrides_config_prefix(monkeypatch, tmp_path, mock_client):
    """Legacy ``container_name_prefix`` config key is silently ignored —
    container names are bare machine names now.
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text('[docker]\ncontainer_name_prefix = "box-"\n')
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg))
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path / "wh"))
    with patch("docker.from_env", return_value=mock_client):
        backend = DockerBackend()
        info = backend.create(name="dev", purpose="t", image="alpine:3")
    assert info.status == "running", f"error: {info.error!r}"
    run_kwargs = mock_client.containers.run.call_args.kwargs
    # Bare machine name — prefix config/env is ignored.
    assert run_kwargs["name"] == "dev"


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
    container.commit.assert_called_once_with(repository="my-image", tag="latest")
    assert result["status"] == "committed"


def test_docker_commit_requires_repo_tag(docker_backend, mock_client):
    """Tag without ':' separator is rejected — prevents silent defaulting."""
    result = docker_backend.commit("dev", "just-a-tag")
    assert result["status"] == "error"
    assert "must be 'repo:tag'" in result["error"]
    mock_client.containers.get.return_value.commit.assert_not_called()


def test_docker_build(docker_backend, tmp_path, monkeypatch):
    """build() in file mode reads Dockerfile from work_home/<machine>/."""
    # Redirect work_home so we don't touch the real home dir.
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    machine_dir = tmp_path / "dev"
    machine_dir.mkdir()
    df = machine_dir / "Dockerfile"
    df.write_text("FROM debian:stable-slim\n")

    with patch.object(docker_backend._ensure_client().images, "build") as mock_build:
        mock_build.return_value = (MagicMock(), [])
        result = docker_backend.build(
            "my-image:latest",
            machine="dev",
            dockerfile="/workspace/Dockerfile",
            context_dir="/workspace",
        )
    assert result["status"] == "built"
    # Verify the SDK got the host path, not the container path.
    build_kwargs = mock_build.call_args.kwargs
    assert build_kwargs["path"] == str(machine_dir)
    assert build_kwargs["dockerfile"] == "Dockerfile"


def test_docker_build_default_paths(docker_backend, tmp_path, monkeypatch):
    """dockerfile and context_dir default to /workspace/Dockerfile and /workspace."""
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    machine_dir = tmp_path / "dev"
    machine_dir.mkdir()
    (machine_dir / "Dockerfile").write_text("FROM debian\n")

    with patch.object(docker_backend._ensure_client().images, "build") as mock_build:
        mock_build.return_value = (MagicMock(), [])
        result = docker_backend.build("img:latest", machine="dev")
    assert result["status"] == "built"
    assert mock_build.call_args.kwargs["path"] == str(machine_dir.resolve())


def test_docker_build_rejects_host_path(docker_backend, tmp_path, monkeypatch):
    """Host paths (anything not under /workspace) are rejected — sandbox boundary."""
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    result = docker_backend.build(
        "img:latest",
        machine="dev",
        dockerfile="/etc/passwd",
        context_dir="/workspace",
    )
    assert result["status"] == "error"
    assert "/workspace" in result["error"]
    assert "sandbox boundary" in result["error"]


def test_docker_build_rejects_host_context(docker_backend, tmp_path, monkeypatch):
    """context_dir outside /workspace is also rejected."""
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    result = docker_backend.build(
        "img:latest",
        machine="dev",
        dockerfile="/workspace/Dockerfile",
        context_dir="/etc",
    )
    assert result["status"] == "error"
    assert "sandbox boundary" in result["error"]


def test_docker_build_nested_workspace_path(docker_backend, tmp_path, monkeypatch):
    """Container paths under /workspace/foo translate to work_home/<machine>/foo."""
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    nested = tmp_path / "dev" / "app" / "Dockerfile"
    nested.parent.mkdir(parents=True)
    nested.write_text("FROM debian\n")

    with patch.object(docker_backend._ensure_client().images, "build") as mock_build:
        mock_build.return_value = (MagicMock(), [])
        result = docker_backend.build(
            "img:latest",
            machine="dev",
            dockerfile="/workspace/app/Dockerfile",
            context_dir="/workspace/app",
        )
    assert result["status"] == "built"
    assert mock_build.call_args.kwargs["path"] == str((tmp_path / "dev" / "app").resolve())


def test_docker_build_missing_dockerfile(docker_backend, tmp_path, monkeypatch):
    """File mode with no Dockerfile on disk → daemon-reported error surfaces.

    The mcp process no longer pre-checks ``df_host.is_file()`` because
    that inspects the wrong filesystem when sandbox-mcp runs inside a
    container (the path is valid on the docker daemon's host but
    invisible to the mcp process).  The daemon is the source of truth;
    its ``BuildError`` / ``NotFound`` is propagated to the agent.
    """
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    (tmp_path / "dev").mkdir()
    with patch.object(
        docker_backend._ensure_client().images,
        "build",
        side_effect=docker.errors.BuildError("Cannot locate specified Dockerfile: Dockerfile", []),
    ):
        result = docker_backend.build("img:latest", machine="dev")
    assert result["status"] == "error"
    assert "Dockerfile" in result["error"]


def test_docker_build_missing_context(docker_backend, tmp_path, monkeypatch):
    """Missing context dir → daemon-reported NotFound surfaces.

    Mirrors ``test_docker_build_missing_dockerfile`` for the context
    directory.  The daemon raises ``NotFound`` (subclass of ``APIError``)
    when the build context path is absent on the host; the agent sees
    the daemon's message verbatim.
    """
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    (tmp_path / "dev").mkdir()
    with patch.object(
        docker_backend._ensure_client().images,
        "build",
        side_effect=docker.errors.NotFound("context not found"),
    ):
        result = docker_backend.build("img:latest", machine="dev")
    assert result["status"] == "error"
    assert "context" in result["error"].lower() or "not found" in result["error"].lower()


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


def test_docker_open_shell_close_bails_on_garbage_socket(docker_backend, mock_client):
    """shell.close() must join the demux/stdin threads quickly even if
    the underlying socket returns something that isn't ``bytes``.

    Defense in depth: if a real docker socket ever returns non-bytes
    (truncated frame, network error, mock test double that returns
    ``MagicMock``), the demux loop would otherwise spin forever on
    ``header += chunk`` (which silently produces a ``MagicMock`` for
    ``b"" + MagicMock``), pegging a CPU core and starving subsequent
    tests in the suite.  ``_done.set()`` in the loop's ``finally`` is
    never reached because the thread is stuck in the inner loop.
    """
    container = mock_client.containers.get.return_value
    container.id = "c123"
    api = mock_client.api
    api.exec_create.return_value = {"Id": "e789"}

    a, b = socket.socketpair()
    socket_mock = MagicMock()
    socket_mock._sock = b
    # Garbage: returns MagicMock, not bytes.  Mirrors what test_docker_open_shell
    # would produce if the mock socket were ever used for an actual read.
    api.exec_start.return_value = socket_mock

    shell = docker_backend.open_shell("dev")
    t0 = time.time()
    shell.close()
    elapsed = time.time() - t0
    a.close()
    b.close()

    assert elapsed < 1.0, (
        f"shell.close() took {elapsed:.2f}s; demux loop likely spinning "
        f"on non-bytes from socket.  See DockerExecProcess._demux_loop."
    )


def test_docker_exec_oneoff(docker_backend, mock_client):
    """exec_oneoff uses SDK exec_run (stdin data embedded in command)."""
    container = mock_client.containers.get.return_value
    container.exec_run.return_value = (0, b"hello\n")
    result = docker_backend.exec_oneoff("dev", "echo hello")
    assert result["exit_code"] == 0
    assert "hello" in result["output"]


def test_docker_list_images(docker_backend, mock_client):
    mock_client.images.list.return_value = []
    assert docker_backend.list_images() == []


def test_docker_create_does_not_reject_sandbox_named_container(docker_backend, mock_client):
    """Containers are no longer name-prefixed; the label is the only
    namespace authority.  ``docker_run(name="sandbox-foo")`` is
    permitted — the label ``sandbox-mcp.managed=true`` is what
    identifies it as ours, regardless of the chosen name.
    """
    info = docker_backend.create(name="sandbox-foo", purpose="t", image="alpine:3")
    assert info.status == "running", f"expected success, got {info!r}"
    assert info.name == "sandbox-foo"
    run_kwargs = mock_client.containers.run.call_args.kwargs
    assert run_kwargs["name"] == "sandbox-foo"
    assert run_kwargs["labels"]["sandbox-mcp.managed"] == "true"


def test_docker_list_images_returns_all_daemon_images(docker_backend, mock_client):
    """``list_images`` is read-only — no production impact from a
    over-broad list — so we no longer filter.  Agents get the daemon's
    full inventory back, the same as the original behaviour before the
    security hardening.
    """
    img_a = MagicMock()
    img_a.tags = ["python:3.12"]
    img_a.short_id = "sha256:aaa"
    img_b = MagicMock()
    img_b.tags = ["internal-payment-api:v2.3"]
    img_b.short_id = "sha256:bbb"
    mock_client.images.list.return_value = [img_a, img_b]

    result = docker_backend.list_images()
    assert {img["tag"] for img in result} == {"python:3.12", "internal-payment-api:v2.3"}


def test_list_managed_containers_filters_by_label(docker_backend, mock_client):
    """``list_managed_containers`` queries the daemon by label, NOT by
    name prefix.  Prefix is a soft hint; the label is the authoritative
    marker.  An attacker who creates a ``sandbox-foo`` container by hand
    (with no label) is invisible to reconciliation — it stays an
    unmanaged host container.
    """
    managed = MagicMock()
    managed.labels = {"sandbox-mcp.managed": "true", "sandbox-mcp.machine": "dev"}
    managed.attrs = {"Created": "2026-01-01T00:00:00Z", "State": {"Status": "running"}}
    unmanaged_same_prefix = MagicMock()
    unmanaged_same_prefix.labels = {}  # no label — attacker-created
    unmanaged_same_prefix.attrs = {"Created": "2026-01-01T00:00:00Z"}
    mock_client.containers.list.return_value = [managed, unmanaged_same_prefix]

    out = docker_backend.list_managed_containers()
    assert out == [("dev", managed.attrs)]
    # The label filter is what matters — name prefix is not consulted.
    mock_client.containers.list.assert_called_once()
    kwargs = mock_client.containers.list.call_args.kwargs
    assert kwargs.get("filters", {}).get("label") == "sandbox-mcp.managed=true"
    assert kwargs.get("all") is True


# ---- auto-network ----


def test_ensure_network_creates_when_not_found(docker_backend, mock_client):
    from docker.errors import NotFound as DockerNotFound

    mock_client.networks.get.side_effect = DockerNotFound("not found")
    docker_backend.ensure_network("sandbox-mcp")
    mock_client.networks.create.assert_called_once_with(
        "sandbox-mcp", driver="bridge", check_duplicate=True
    )


def test_ensure_network_noop_when_exists(docker_backend, mock_client):
    mock_client.networks.get.return_value = MagicMock()
    docker_backend.ensure_network("sandbox-mcp")
    mock_client.networks.create.assert_not_called()


def test_ensure_network_empty_name_is_noop(docker_backend, mock_client):
    docker_backend.ensure_network("")
    mock_client.networks.get.assert_not_called()
    mock_client.networks.create.assert_not_called()


def test_ensure_network_swallows_race_during_create(docker_backend, mock_client):
    from docker.errors import APIError

    mock_client.networks.get.side_effect = APIError("something")
    docker_backend.ensure_network("sandbox-mcp")
    # swallow — not a fatal path


def test_create_passes_auto_network(docker_backend, mock_client, monkeypatch):
    """create() passes network=auto_network to containers.run."""
    from docker.errors import APIError as DockerAPIError

    mock_client.networks.get.side_effect = DockerAPIError("mock")
    info = docker_backend.create(name="dev", purpose="t", image="alpine:3")
    assert info.status == "running"
    run_kwargs = mock_client.containers.run.call_args.kwargs
    assert run_kwargs.get("network") == "sandbox-mcp"


def test_create_empty_auto_network_omits_network(
    docker_backend, mock_client, monkeypatch, tmp_path
):
    """When auto_network is empty, containers.run gets network=None."""
    monkeypatch.setenv("SANDBOX_MCP_DOCKER_AUTO_NETWORK", "")
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    info = docker_backend.create(name="dev", purpose="t", image="alpine:3")
    assert info.status == "running"
    run_kwargs = mock_client.containers.run.call_args.kwargs
    assert run_kwargs.get("network") is None


# ---- create() idempotent reattach on name conflict (409) ----


def _conflict_error():
    """A docker APIError whose status_code is 409 (name already in use).

    The docker SDK parses the daemon's JSON body into ``explanation``;
    create() renders the error via ``e.explanation or e``, so we set it
    to the realistic conflict message.
    """
    from docker.errors import APIError

    return APIError(
        "Conflict. The container name '/dev' is already in use",
        response=MagicMock(status_code=409),
        explanation="Conflict. The container name '/dev' is already in use by container abc123",
    )


def test_docker_create_reattaches_running_container_on_conflict(docker_backend, mock_client):
    """A 409 from containers.run reattaches to the existing *running*
    container instead of erroring - the idempotent docker_run contract.
    The response carries a note so the agent knows it was a reuse.
    """
    mock_client.containers.run.side_effect = _conflict_error()
    existing = mock_client.containers.get.return_value
    existing.attrs = {"State": {"Status": "running"}}
    # Existing container carries its own purpose label (the truth on reattach).
    existing.labels = {"sandbox-mcp.purpose": "t"}

    info = docker_backend.create(name="dev", purpose="t", image="alpine:3")

    assert info.status == "running", f"expected reattach, got {info!r}"
    assert info.name == "dev"
    assert info.purpose == "t"  # existing label's purpose, kept
    assert "reattached" in info.note
    assert "already running" in info.note
    # Purposes match -> no mismatch hint.
    assert "ignored" not in info.note
    # run was attempted (and conflicted); get located the existing container.
    mock_client.containers.run.assert_called_once()
    mock_client.containers.get.assert_called_once_with("dev")
    # Already running -> no start.
    existing.start.assert_not_called()


def test_docker_create_reattaches_and_starts_stopped_container(docker_backend, mock_client):
    """A 409 against a *stopped* container starts it, then reports running.

    reload() flips the daemon's reported state to "running" after start
    (the mock simulates this); _running_info confirms it before reporting.
    """
    mock_client.containers.run.side_effect = _conflict_error()
    existing = MagicMock()
    existing.attrs = {"State": {"Status": "exited"}}
    existing.labels = {"sandbox-mcp.purpose": "t"}

    def _reload():
        existing.attrs = {"State": {"Status": "running"}}

    existing.reload.side_effect = _reload
    mock_client.containers.get.return_value = existing

    info = docker_backend.create(name="dev", purpose="t", image="alpine:3")

    assert info.status == "running"
    existing.start.assert_called_once_with()
    assert "reattached" in info.note
    assert "started" in info.note


def test_docker_create_reattach_notes_purpose_mismatch(docker_backend, mock_client):
    """Docker labels are immutable, so a reattach CANNOT adopt a new
    purpose.  The existing label's purpose is kept; if the caller passed
    a different non-empty purpose, a note flags it (remove+recreate to
    change).  The returned TargetInfo carries the EXISTING purpose.
    """
    mock_client.containers.run.side_effect = _conflict_error()
    existing = mock_client.containers.get.return_value
    existing.attrs = {"State": {"Status": "running"}}
    existing.labels = {"sandbox-mcp.purpose": "old-purpose"}

    info = docker_backend.create(name="dev", purpose="new-purpose", image="alpine:3")

    assert info.status == "running"
    # Existing purpose wins, not the caller's.
    assert info.purpose == "old-purpose"
    assert "reattached" in info.note
    assert "ignored" in info.note
    assert "'new-purpose'" in info.note
    assert "'old-purpose'" in info.note
    assert "remove+recreate" in info.note


def test_docker_create_reattach_no_mismatch_note_when_purpose_empty(docker_backend, mock_client):
    """Caller passes no purpose (empty) -> no mismatch note even if the
    existing container has one (the caller didn't ask to set one)."""
    mock_client.containers.run.side_effect = _conflict_error()
    existing = mock_client.containers.get.return_value
    existing.attrs = {"State": {"Status": "running"}}
    existing.labels = {"sandbox-mcp.purpose": "old"}

    info = docker_backend.create(name="dev", purpose="", image="alpine:3")
    assert info.status == "running"
    assert info.purpose == "old"
    assert "ignored" not in info.note


def test_docker_create_reattach_start_fails_to_run(docker_backend, mock_client):
    """Reattach starts a stopped container, but its command crashes
    immediately -- create() reports the error with a diagnostic, NOT
    "running".  start() returning is not a guarantee the container stays up.
    """
    mock_client.containers.run.side_effect = _conflict_error()
    existing = MagicMock()
    existing.attrs = {"State": {"Status": "exited", "ExitCode": 1}}
    existing.labels = {"sandbox-mcp.purpose": "t"}
    existing.logs.return_value = b"boom: missing dependency\n"
    # reload() leaves attrs as-is (still exited) -- the container died.
    mock_client.containers.get.return_value = existing

    info = docker_backend.create(name="dev", purpose="t", image="alpine:3")
    assert info.status == "error"
    assert "exited" in info.error
    assert "exit_code=1" in info.error
    assert "boom: missing dependency" in info.error


def test_docker_start_returns_error_when_container_exits(docker_backend, mock_client):
    """docker_start must verify the container is actually running.  A
    container whose command crashes exits within milliseconds of start();
    reporting "running" would mislead the agent into shelling into a
    dead container.  Instead surface status + exit code + a log tail.
    """
    container = mock_client.containers.get.return_value
    container.attrs = {"State": {"Status": "exited", "ExitCode": 127}}
    container.logs.return_value = b"sleep: command not found\n"

    info = docker_backend.start("dev")
    container.start.assert_called_once()
    assert info.status == "error"
    assert "exited" in info.error
    assert "exit_code=127" in info.error
    assert "sleep: command not found" in info.error


def test_docker_create_conflict_then_get_notfound_returns_error(docker_backend, mock_client):
    """Race: run reports 409 but the container is gone by the get() lookup.
    Reattach returns None and the original conflict error surfaces.
    """
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.run.side_effect = _conflict_error()
    mock_client.containers.get.side_effect = DockerNotFound("vanished")

    info = docker_backend.create(name="dev", purpose="t", image="alpine:3")
    assert info.status == "error"
    assert "already in use" in str(info.error)


def test_docker_create_non_conflict_apierror_does_not_reattach(docker_backend, mock_client):
    """A non-409 APIError (e.g. daemon 500) is a real error - no reattach."""
    from docker.errors import APIError

    mock_client.containers.run.side_effect = APIError("boom", response=MagicMock(status_code=500))

    info = docker_backend.create(name="dev", purpose="t", image="alpine:3")
    assert info.status == "error"
    # Reattach path never runs, so containers.get is never consulted.
    mock_client.containers.get.assert_not_called()


def test_docker_create_image_not_found_returns_specific_error(docker_backend, mock_client):
    """ImageNotFound is a subclass of APIError; it must be caught before
    the APIError branch so the 'image not found' message is preserved
    (previously the broader APIError handler swallowed it).
    """
    from docker.errors import ImageNotFound

    mock_client.containers.run.side_effect = ImageNotFound("nope")
    info = docker_backend.create(name="dev", purpose="test", image="nonexistent:latest")
    assert info.status == "error"
    assert "Image nonexistent:latest not found" in str(info.error)


def test_docker_inspect_returns_curated_view(docker_backend, mock_client):
    """Curated inspect returns a flattened, agent-friendly dict with only
    fields `shell_exec` can't easily surface (state, cmd, mounts, labels,
    restart_policy).  Env / working_dir / user / network IP are deliberately
    omitted — those are `shell_exec env` / `pwd` / `whoami` / `hostname -i`.
    """
    container = mock_client.containers.get.return_value
    container.attrs = {
        "State": {
            "Status": "running",
            "Running": True,
            "ExitCode": 0,
            "Error": "",
            "RestartCount": 2,
            "StartedAt": "2026-07-16T10:00:01Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
            "Health": {"Status": "healthy"},
        },
        "Config": {
            "Image": "python:3.12-slim",
            "Cmd": ["python", "-m", "http.server"],
            "Entrypoint": None,
            "Labels": {
                "sandbox-mcp.managed": "true",
                "sandbox-mcp.machine": "dev",
                "sandbox-mcp.purpose": "Python dev",
            },
        },
        "Created": "2026-07-16T10:00:00Z",
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}},
        "Mounts": [
            {"Source": "/host/x", "Destination": "/workspace", "Mode": "rw"},
            {"Source": "/host/share", "Destination": "/share", "Mode": "ro"},
        ],
    }
    container.short_id = "abc123def456"

    result = docker_backend.inspect("dev")

    assert result["id"] == "abc123def456"
    assert result["name"] == "dev"
    assert result["image"] == "python:3.12-slim"
    assert result["created"] == "2026-07-16T10:00:00Z"
    assert result["started_at"] == "2026-07-16T10:00:01Z"
    assert result["finished_at"] == "0001-01-01T00:00:00Z"
    assert result["state"]["status"] == "running"
    assert result["state"]["running"] is True
    assert result["state"]["exit_code"] == 0
    assert result["state"]["restart_count"] == 2
    assert result["state"]["health"] == "healthy"
    assert result["cmd"] == ["python", "-m", "http.server"]
    assert result["entrypoint"] is None
    assert result["mounts"] == [
        {"source": "/host/x", "destination": "/workspace", "mode": "rw"},
        {"source": "/host/share", "destination": "/share", "mode": "ro"},
    ]
    assert result["labels"]["sandbox-mcp.purpose"] == "Python dev"
    assert result["restart_policy"] == {"name": "unless-stopped", "max_retry": 0}
    # Deliberately omitted (shell_exec can answer):
    assert "env" not in result
    assert "working_dir" not in result
    assert "user" not in result
    assert "network" not in result


def test_docker_inspect_raw_returns_full_attrs(docker_backend, mock_client):
    """raw=True returns the unfiltered attrs dict — agent can read every
    key including Config.Env values and full NetworkSettings."""
    container = mock_client.containers.get.return_value
    container.attrs = {
        "State": {"Status": "running"},
        "Config": {
            "Image": "alpine:3",
            "Env": ["POSTGRES_PASSWORD=hunter2"],
            "Cmd": ["sleep", "infinity"],
        },
    }

    result = docker_backend.inspect("dev", raw=True)

    assert result is container.attrs
    assert result["Config"]["Env"] == ["POSTGRES_PASSWORD=hunter2"]


def test_docker_inspect_container_not_found(docker_backend, mock_client):
    """Container that disappears between calls returns error dict, not raise."""
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.get.side_effect = DockerNotFound("not here")

    result = docker_backend.inspect("ghost")

    assert result["status"] == "error"
    assert "not here" in result["error"]


def test_docker_inspect_image_returns_curated_view(docker_backend, mock_client):
    """kind='image' returns image identity + config (env KEYS only, no values)."""
    image = mock_client.images.get.return_value
    image.short_id = "abc123def456"
    image.tags = ["python:3.12", "python:latest"]
    image.attrs = {
        "Created": "2024-01-15T10:00:00Z",
        "Size": 123_456_789,
        "Architecture": "amd64",
        "Os": "linux",
        "Config": {
            "Cmd": ["python"],
            "Entrypoint": ["/usr/local/bin/python"],
            "Env": [
                "PATH=/usr/local/bin:/usr/bin",
                "PYTHON_VERSION=3.12.0",
                "LANG=C.UTF-8",
            ],
            "ExposedPorts": {"8080/tcp": {}, "9090/udp": {}},
            "Volumes": {"/data": {}, "/logs": {}},
            "Labels": {"maintainer": "psf"},
            "WorkingDir": "/app",
            "User": "nobody",
        },
    }

    result = docker_backend.inspect("python:3.12", kind="image")

    assert result["id"] == "abc123def456"
    assert result["tags"] == ["python:3.12", "python:latest"]
    assert result["size_bytes"] == 123_456_789
    assert result["cmd"] == ["python"]
    assert result["entrypoint"] == ["/usr/local/bin/python"]
    # env_keys must contain NAMES only, never VALUES
    assert "PATH" in result["env_keys"]
    assert "PYTHON_VERSION" in result["env_keys"]
    assert "/usr/local/bin" not in str(result["env_keys"])
    assert "3.12.0" not in str(result["env_keys"])
    assert result["exposed_ports"] == ["8080/tcp", "9090/udp"]
    assert result["volumes"] == ["/data", "/logs"]
    assert result["working_dir"] == "/app"
    assert result["user"] == "nobody"
    mock_client.images.get.assert_called_once_with("python:3.12")


def test_docker_inspect_image_raw_returns_full_attrs(docker_backend, mock_client):
    """kind='image' with raw=True returns the full image attrs dict."""
    image = mock_client.images.get.return_value
    image.attrs = {"Id": "sha256:abc", "Config": {"Env": ["SECRET=real-value"]}}

    result = docker_backend.inspect("img", kind="image", raw=True)

    assert result == image.attrs
    mock_client.images.get.assert_called_once_with("img")


def test_docker_inspect_image_not_found(docker_backend, mock_client):
    """kind='image' with unknown image returns error dict (not raise)."""
    from docker.errors import ImageNotFound

    mock_client.images.get.side_effect = ImageNotFound("nope")

    result = docker_backend.inspect("ghost", kind="image")

    assert result["status"] == "error"
    assert "nope" in result["error"]


def test_docker_inspect_unknown_kind_rejected(docker_backend):
    """kind must be 'container' or 'image'; anything else returns error."""
    result = docker_backend.inspect("anything", kind="volume")
    assert result["status"] == "error"
    assert "unknown inspect kind" in result["error"]


# ---- history() ----


def test_docker_logs_default_tail_200(docker_backend, mock_client):
    """Default tail is 200 lines."""
    container = mock_client.containers.get.return_value
    container.logs.return_value = b"line1\nline2\n"

    result = docker_backend.logs("dev")

    container.logs.assert_called_once_with(tail=200, since=None, until=None, timestamps=False)
    assert result["logs"] == "line1\nline2\n"
    assert result["truncated"] is False


def test_docker_logs_passes_through_filters(docker_backend, mock_client):
    """since/until/timestamps/tail flow through to the SDK."""
    container = mock_client.containers.get.return_value
    container.logs.return_value = b""

    docker_backend.logs(
        "dev",
        tail=50,
        since="2026-07-16T10:00:00Z",
        until="10m",
        timestamps=True,
    )

    container.logs.assert_called_once_with(
        tail=50,
        since="2026-07-16T10:00:00Z",
        until="10m",
        timestamps=True,
    )


def test_docker_logs_rejects_tail_above_cap(docker_backend, mock_client):
    """Tail > 10000 is rejected with error (prevents token-bombing)."""
    result = docker_backend.logs("dev", tail=99999)
    assert result["status"] == "error"
    assert "10000" in result["error"]


def test_docker_logs_container_not_found(docker_backend, mock_client):
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.get.side_effect = DockerNotFound("nope")

    result = docker_backend.logs("ghost")
    assert result["status"] == "error"


def test_docker_logs_decodes_bytes_with_replacement(docker_backend, mock_client):
    """Garbage bytes don't crash — utf-8 with errors='replace' inserts U+FFFD."""
    container = mock_client.containers.get.return_value
    container.logs.return_value = b"good \xff\xfe bad"

    result = docker_backend.logs("dev")
    assert "good" in result["logs"]
    assert "bad" in result["logs"]
    assert "\ufffd" in result["logs"], f"expected U+FFFD replacement char in {result['logs']!r}"


def test_docker_logs_marks_truncated_when_line_count_meets_tail(docker_backend, mock_client):
    """truncated=True when the daemon returned at least `tail` newlines —
    a soft signal that the output was clipped."""
    container = mock_client.containers.get.return_value
    container.logs.return_value = b"a\nb\nc\n"  # 3 lines, 3 newlines

    result = docker_backend.logs("dev", tail=2)

    assert result["truncated"] is True


def test_docker_logs_marks_not_truncated_when_under_tail(docker_backend, mock_client):
    """truncated=False when output has fewer lines than tail."""
    container = mock_client.containers.get.return_value
    container.logs.return_value = b"a\nb\n"  # 2 lines, 2 newlines

    result = docker_backend.logs("dev", tail=5)

    assert result["truncated"] is False


# ---- diff() ----


def test_docker_diff_groups_by_kind(docker_backend, mock_client):
    """diff() returns changes grouped by A (added) / C (changed) / D (deleted)."""
    container = mock_client.containers.get.return_value
    container.diff.return_value = [
        {"Path": "/workspace/new.txt", "Kind": 1},  # Added
        {"Path": "/workspace/modified.yaml", "Kind": 0},  # Changed
        {"Path": "/workspace/old.log", "Kind": 2},  # Deleted
        {"Path": "/workspace/another_new.txt", "Kind": 1},  # Added
    ]

    result = docker_backend.diff("dev")

    assert result["changes"]["A"] == ["/workspace/another_new.txt", "/workspace/new.txt"]
    assert result["changes"]["C"] == ["/workspace/modified.yaml"]
    assert result["changes"]["D"] == ["/workspace/old.log"]
    assert result["summary"] == {"added": 2, "changed": 1, "deleted": 1}


def test_docker_diff_empty_returns_empty_groups(docker_backend, mock_client):
    """A clean container (no fs changes) returns three empty groups."""
    container = mock_client.containers.get.return_value
    container.diff.return_value = []

    result = docker_backend.diff("dev")

    assert result["changes"] == {"A": [], "C": [], "D": []}
    assert result["summary"] == {"added": 0, "changed": 0, "deleted": 0}


def test_docker_diff_container_not_found(docker_backend, mock_client):
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.get.side_effect = DockerNotFound("nope")

    result = docker_backend.diff("ghost")
    assert result["status"] == "error"


def test_docker_stats_returns_snapshot(docker_backend, mock_client):
    """stats() returns a curated one-shot snapshot — no streaming."""
    container = mock_client.containers.get.return_value
    container.stats.return_value = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200_000_000, "percpu_usage": [100_000_000, 100_000_000]},
            "system_cpu_usage": 10_000_000_000,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100_000_000},
            "system_cpu_usage": 9_900_000_000,
        },
        "memory_stats": {"usage": 50 * 1024 * 1024, "limit": 1024 * 1024 * 1024},
        "networks": {
            "eth0": {"rx_bytes": 1234, "tx_bytes": 5678},
            "eth1": {"rx_bytes": 100, "tx_bytes": 200},
        },
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "Read", "value": 4096},
                {"op": "Write", "value": 8192},
            ]
        },
    }

    result = docker_backend.stats("dev")

    container.stats.assert_called_once_with(stream=False)
    # CPU: cpu_delta=100_000_000, system_delta=100_000_000, num_cpus=2
    # cpu% = (100e6 / 100e6) * 2 * 100 = 200%
    assert result["cpu_percent"] == pytest.approx(200.0)
    # Memory
    assert result["memory"]["usage_bytes"] == 50 * 1024 * 1024
    assert result["memory"]["limit_bytes"] == 1024 * 1024 * 1024
    expected_pct = 50 * 1024 * 1024 / (1024 * 1024 * 1024) * 100
    assert result["memory"]["usage_percent"] == pytest.approx(expected_pct)
    # Network — aggregated across interfaces
    assert result["network"]["rx_bytes"] == 1234 + 100
    assert result["network"]["tx_bytes"] == 5678 + 200
    # Block IO — split by op
    assert result["block_io"]["read_bytes"] == 4096
    assert result["block_io"]["write_bytes"] == 8192


def test_docker_stats_container_not_found(docker_backend, mock_client):
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.get.side_effect = DockerNotFound("nope")

    result = docker_backend.stats("ghost")
    assert result["status"] == "error"


def test_docker_stats_handles_zero_cpu_delta(docker_backend, mock_client):
    """First sample has zero deltas; cpu_percent is 0.0 (no divide-by-zero)."""
    container = mock_client.containers.get.return_value
    container.stats.return_value = {
        "cpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
        "precpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
        "memory_stats": {},
        "networks": {},
        "blkio_stats": {},
    }

    result = docker_backend.stats("dev")
    assert result["cpu_percent"] == 0.0
    assert result["memory"] == {"usage_bytes": 0, "limit_bytes": 0, "usage_percent": 0.0}
    assert result["network"] == {"rx_bytes": 0, "tx_bytes": 0}
    assert result["block_io"] == {"read_bytes": 0, "write_bytes": 0}


# ---- restart() ----


def test_docker_restart_succeeds(docker_backend, mock_client):
    """A clean restart reports 'running' and uses the default 10s timeout."""
    container = mock_client.containers.get.return_value
    container.attrs = {"State": {"Status": "running", "Running": True}}

    info = docker_backend.restart("dev")

    container.restart.assert_called_once_with(timeout=10)
    assert info.name == "dev"
    assert info.status == "running"


def test_docker_restart_passes_timeout(docker_backend, mock_client):
    """Custom timeout flows to container.restart()."""
    container = mock_client.containers.get.return_value
    container.attrs = {"State": {"Status": "running", "Running": True}}

    docker_backend.restart("dev", timeout=30)
    container.restart.assert_called_once_with(timeout=30)


def test_docker_restart_container_not_found(docker_backend, mock_client):
    from docker.errors import NotFound as DockerNotFound

    mock_client.containers.get.side_effect = DockerNotFound("nope")

    info = docker_backend.restart("ghost")
    assert info.status == "error"


def test_docker_restart_surfaces_crash_diagnostic(docker_backend, mock_client):
    """Container whose CMD crashes post-restart reports error + log tail,
    not 'running' (matches _running_info post-check semantics)."""
    container = mock_client.containers.get.return_value
    container.attrs = {"State": {"Status": "exited", "ExitCode": 127}}
    container.logs.return_value = b"sleep: command not found\n"

    info = docker_backend.restart("dev")

    assert info.status == "error"
    assert "exited" in info.error
    assert "exit_code=127" in info.error
    assert "sleep: command not found" in info.error


def test_docker_restart_error_says_after_restart_not_after_start(docker_backend, mock_client):
    """The diagnostic message from _running_info must say 'after restart'
    when invoked by docker_restart, not 'after start' (the operation
    was a restart, not a bare start)."""
    container = mock_client.containers.get.return_value
    container.attrs = {"State": {"Status": "exited", "ExitCode": 1}}
    container.logs.return_value = b""

    info = docker_backend.restart("dev")

    assert info.status == "error"
    assert "after restart" in info.error
    assert "after start" not in info.error


def test_docker_history_returns_layers(docker_backend, mock_client):
    """history() returns structured layer info from docker SDK history()."""
    image = mock_client.images.get.return_value
    image.history.return_value = [
        {
            "Id": "sha256:aaa111aaa111aaa111aaa111aaa111aaa111aaa111aaa111aaa111aaa111aaa1",
            "Created": 1700000000,
            "CreatedBy": "/bin/sh -c apt-get install -y python",
            "Size": 50_000_000,
            "Tags": ["python:3.12"],
        },
        {
            "Id": "sha256:bbb222bbb222bbb222bbb222bbb222bbb222bbb222bbb222bbb222bbb222bbb2",
            "Created": 1699999000,
            "CreatedBy": "COPY ./app /app  # buildkit",
            "Size": 5_000_000,
            "Tags": [],
        },
    ]

    result = docker_backend.history("python:3.12")

    assert result["image"] == "python:3.12"
    assert result["layer_count"] == 2
    assert result["total_size_bytes"] == 55_000_000
    assert len(result["layers"]) == 2
    # IDs are 12-char prefixes, NOT full shas (avoids token bloat)
    assert result["layers"][0]["id"] == "aaa111aaa111"
    assert result["layers"][0]["created_by"] == "/bin/sh -c apt-get install -y python"
    assert result["layers"][1]["tags"] == []
    mock_client.images.get.assert_called_once_with("python:3.12")


def test_docker_history_image_not_found(docker_backend, mock_client):
    """history() with unknown image returns error dict."""
    from docker.errors import ImageNotFound

    mock_client.images.get.side_effect = ImageNotFound("nope")

    result = docker_backend.history("ghost:latest")

    assert result["status"] == "error"
    assert "nope" in result["error"]


def test_docker_build_wraps_typeerror(docker_backend, mock_client):
    """Docker SDK raises TypeError when path is not a directory; surface a hint."""
    mock_client.images.build.side_effect = TypeError(
        "You must specify a directory to build in path"
    )

    result = docker_backend.build("img:latest", machine="dev", dockerfile="/workspace/Dockerfile")

    assert result["status"] == "error"
    # The error must point to context_dir / dockerfile as the likely cause,
    # not just echo the opaque SDK message.
    assert "context_dir" in result["error"]
    assert "/workspace/Dockerfile" in result["error"]
    # AND it must mention the compose bind-mount requirement so an operator
    # whose mcp container doesn't see work_home has an actionable hint
    # instead of the opaque SDK error.
    assert "bind-mounted" in result["error"] or "bind mount" in result["error"]


def test_docker_exec_process_poll_returns_running_and_exit():
    """DockerExecProcess.poll() returns None while running, ExitCode when done.

    Used by ShellSession._drain to capture exit_reason / last_exit_code
    so the next shell can report why the previous one died.
    """
    from unittest.mock import MagicMock

    from sandbox_mcp.backends.docker_backend import DockerExecProcess

    api = MagicMock()
    api.exec_create.return_value = {"Id": "exec-abc"}

    container = MagicMock()
    container.client.api = api
    container.id = "c123"

    # Pipe fds so __init__ doesn't crash.
    import os

    _r_in, _w_in = os.pipe()
    _r_out, _w_out = os.pipe()

    # Replace sock with a real socket pair so sendall/recv don't crash.
    import socket

    a, _b = socket.socketpair()
    container.client.api.exec_start.return_value = type("S", (), {"_sock": a})()

    proc = DockerExecProcess(container, ["bash"])

    # While running: poll() returns None.
    api.exec_inspect.return_value = {"Running": True, "ExitCode": None}
    assert proc.poll() is None

    # After exit: poll() returns ExitCode.
    api.exec_inspect.return_value = {"Running": False, "ExitCode": 42}
    assert proc.poll() == 42


def test_docker_compose_mounts_work_home():
    """docker-compose.yml must bind-mount WORK_HOME so docker_build works.

    Regression: the SDK's ``images.build(path=...)`` tar-walks the build
    context in the SDK client process before POSTing to the daemon. When
    sandbox-mcp runs in a container, that walk happens in the mcp
    container's filesystem — so work_home has to be mounted there. The
    env var ``SANDBOX_MCP_STORAGE_WORK_HOME`` must agree with the mount
    target, otherwise ``get_work_home()`` returns a different path than
    the one bind-mounted and the SDK's ``os.path.isdir`` check fails.
    """
    compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
    compose = compose_path.read_text()

    # The bind mount: ${WORK_HOME:-<default>}:${WORK_HOME:-<default>}
    assert "${WORK_HOME:-/var/lib/sandbox-mcp}:${WORK_HOME:-/var/lib/sandbox-mcp}" in compose, (
        "docker-compose.yml must bind-mount WORK_HOME into the mcp container "
        "so docker_build can tar-walk the context."
    )
    # The env override: get_work_home() must return the bind-mounted path.
    assert "SANDBOX_MCP_STORAGE_WORK_HOME: ${WORK_HOME:-/var/lib/sandbox-mcp}" in compose, (
        "docker-compose.yml must export SANDBOX_MCP_STORAGE_WORK_HOME so the "
        "in-container path matches the mount."
    )
