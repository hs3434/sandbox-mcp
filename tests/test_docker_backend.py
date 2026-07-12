import socket
import time
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
    # Volume mounts: only the auto-bound work_home mount is allowed;
    # no agent-supplied host paths leak through (security boundary).
    mounts = run_kwargs.get("volumes") or {}
    assert len(mounts) == 1, f"expected only the work_home mount, got: {mounts}"
    # Reconciliation labels are set on every container this backend creates —
    # this is how SandboxServer re-discovers them after restart.
    labels = run_kwargs.get("labels") or {}
    assert labels.get("sandbox-mcp.managed") == "true"
    assert labels.get("sandbox-mcp.machine") == "dev"


def test_docker_create_ignores_volumes_kwarg(docker_backend, mock_client):
    """Agent cannot smuggle arbitrary host paths into the container via
    a ``volumes`` kwarg.  The only bind mount is the auto-attached
    work_home; agent-supplied mounts are silently dropped.
    """
    docker_backend.create(
        name="dev",
        purpose="test",
        image="python:3.12",
        volumes=["/etc:/host-etc", "/root:/host-root"],  # attacker attempt
    )
    mounts = mock_client.containers.run.call_args.kwargs.get("volumes") or {}
    # Only one mount allowed: the work_home for "dev".
    assert len(mounts) == 1, f"agent-supplied mounts leaked: {mounts}"
    # The single mount must end with the work_home dir for "dev", not /etc or /root.
    host_path = next(iter(mounts))
    assert str(host_path).endswith("/dev"), f"unexpected mount host path: {host_path}"


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


def test_docker_build_rejects_inline_dockerfile_content(docker_backend, tmp_path, monkeypatch):
    """Agent cannot supply a Dockerfile out-of-band via ``dockerfile_content``.

    Inline mode used to stage the Dockerfile under ``work_home/_builds/``
    and feed it directly to ``docker build`` — bypassing the sandbox's
    file-write audit trail AND dodging the work_home visibility check.
    A malicious inline Dockerfile (``RUN --mount=type=bind,source=/,...``)
    executes in a daemon-orchestrated container with full host kernel
    capabilities, so inline mode is a host-RCE vector.

    File mode (the only remaining path) requires the agent to have
    written the Dockerfile via ``sandbox_file_write`` into
    ``/workspace/Dockerfile``, which is itself bound from work_home.
    """
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    result = docker_backend.build("img:latest", dockerfile_content="FROM scratch\n")
    assert result["status"] == "error", f"inline mode should be rejected, got {result!r}"
    assert "dockerfile_content" in result["error"].lower()
    # The build SDK must not have been called.
    docker_backend._ensure_client().images.build.assert_not_called()


def test_docker_build_file_mode_requires_machine(docker_backend):
    """File mode without dockerfile_content and without machine → error."""
    result = docker_backend.build("img:latest")
    assert result["status"] == "error"
    assert "machine is required" in result["error"]


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
    """File mode with no Dockerfile on disk → error (not SDK exception)."""
    monkeypatch.setenv("SANDBOX_MCP_STORAGE_WORK_HOME", str(tmp_path))
    (tmp_path / "dev").mkdir()
    result = docker_backend.build("img:latest", machine="dev")
    assert result["status"] == "error"
    assert "Dockerfile not found" in result["error"]


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


def test_docker_list_images_returns_only_managed_and_built(docker_backend, mock_client):
    """``list_images()`` must not leak the host's full image inventory.

    Only images that are either (a) in use by a registered sandbox
    machine or (b) built via ``docker_build`` are visible to the agent.
    The daemon may host private / internal / unrelated images; the
    agent has no business seeing them.
    """
    # Image #1: in use by a registered container (returns short_id only).
    in_use_image = MagicMock()
    in_use_image.tags = []  # pulled without tagging, e.g. "postgres:16" by digest
    in_use_image.short_id = "sha256:aaa111"
    # Image #2: built via docker_build (recorded in _built_images).
    built_image = MagicMock()
    built_image.tags = ["myapp:v1"]
    built_image.short_id = "sha256:bbb222"
    # Image #3: present on host but NEITHER in use NOR built — must be filtered out.
    leaked_image = MagicMock()
    leaked_image.tags = ["internal-payment-api:v2.3"]
    leaked_image.short_id = "sha256:ccc333"
    mock_client.images.list.return_value = [in_use_image, built_image, leaked_image]

    docker_backend._built_images = {"myapp:v1"}  # type: ignore[attr-defined]
    docker_backend._managed_images = {"sha256:aaa111"}  # type: ignore[attr-defined]

    result = docker_backend.list_images()
    ids = {img["image_id"] for img in result}
    assert ids == {"sha256:aaa111", "sha256:bbb222"}, f"unmanaged images leaked to agent: {result}"


def test_docker_create_rejects_name_with_prefix(docker_backend, mock_client):
    """``docker_run(name="sandbox-foo")`` would create ``sandbox-sandbox-foo`` —
    confusing because the agent looks like it's reaching for a host-managed
    container.  Reject names that already start with the configured prefix.
    """
    info = docker_backend.create(name="sandbox-attacker", purpose="t", image="alpine:3")
    assert info.status == "error", f"expected rejection, got {info!r}"
    assert "prefix" in info.error.lower()
    mock_client.containers.run.assert_not_called()


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
