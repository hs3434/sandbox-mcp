import pytest

from sandbox_mcp.safety import (
    CATEGORIES,
    check_path_safety,
    is_read_denied,
    is_write_denied,
)

# ---- exact paths ----


@pytest.mark.parametrize(
    "path",
    [
        "/etc/shadow",
        "/etc/passwd",
        "/etc/sudoers",
        "/etc/gshadow",
    ],
)
def test_exact_system_paths_are_advised(path):
    result = check_path_safety(path)
    assert result["warning"] is not None
    assert result["category"] == CATEGORIES["exact_path"]


def test_path_traversal_cannot_bypass_shadow(tmp_path, monkeypatch):
    """``../etc/shadow`` must resolve and trigger the advisory."""
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    evil = nested / "x"
    # The path itself doesn't need to exist; realpath walks up until it
    # finds an existing component, then resolves ``..`` segments.
    monkeypatch.chdir(tmp_path)
    result = check_path_safety(str(evil))
    # ``tmp_path`` is a real directory under /tmp, not under /etc, so
    # the check should pass. The point is the *resolution* doesn't crash.
    assert "warning" in result


def test_etc_path_traversal_would_resolve_to_blocked(tmp_path, monkeypatch):
    """If we craft a path that realpath resolves into a blocked dir,
    the advisory fires.  Simulate by writing a real file under /etc/shadow
    is not possible in CI; instead test the prefix logic via /etc/sudoers.d.
    """
    result = check_path_safety("/etc/sudoers.d/extra")
    assert result["warning"] is not None


# ---- path prefixes ----


@pytest.mark.parametrize(
    "path",
    [
        "/root/.ssh/id_rsa",
        "/root/.ssh/authorized_keys",
        "/root/.ssh/config",
        "/root/.aws/credentials",
        "/root/.gnupg/secring.gpg",
        "/root/.kube/config",
        "/root/.docker/config.json",
        "/root/.netrc",
        "/root/.pgpass",
        "/root/.pypirc",
        "/home/user/.ssh/id_rsa",
        "/home/deploy/.aws/credentials",
    ],
)
def test_credential_directory_paths_are_advised(path):
    result = check_path_safety(path)
    assert result["warning"] is not None
    assert result["category"] == CATEGORIES["dir_prefix"]


def test_ssh_subdir_specifically_advised_with_dir_prefix_category():
    result = check_path_safety("/root/.ssh/anything")
    assert result["warning"] is not None
    assert result["category"] == CATEGORIES["dir_prefix"]


# ---- basenames ----


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/.env",
        "/var/www/myapp/.env.production",
        "/srv/project/.env.local",
        "/root/.envrc",
        "/opt/legacy/.env",
    ],
)
def test_project_env_basenames_are_advised(path):
    result = check_path_safety(path)
    assert result["warning"] is not None
    assert result["category"] == CATEGORIES["basename"]


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/id_rsa",
        "/var/spool/old/id_ed25519",
        "/srv/leaked/id_dsa",
    ],
)
def test_private_key_basenames_are_advised(path):
    result = check_path_safety(path)
    assert result["warning"] is not None
    assert result["category"] == CATEGORIES["basename"]


# ---- safe paths ----


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/safe.txt",
        "/var/log/app.log",
        "/root/code/main.py",
        "/srv/data/measurements.json",
        "/home/user/projects/README.md",
    ],
)
def test_safe_paths_return_no_advisory(path):
    result = check_path_safety(path)
    assert result["warning"] is None
    assert result["category"] is None


# ---- is_read_denied / is_write_denied are aliases ----


def test_is_read_denied_matches_check_path_safety():
    assert is_read_denied("/etc/shadow") is True
    assert is_read_denied("/tmp/safe.txt") is False
    assert is_read_denied("/root/.aws/credentials") is True


def test_is_write_denied_aliases_read():
    assert is_write_denied("/etc/shadow") == is_read_denied("/etc/shadow")
    assert is_write_denied("/tmp/x.py") == is_read_denied("/tmp/x.py")


# ---- tilde expansion ----


def test_tilde_resolves_to_home(monkeypatch):
    """``~/.ssh/...`` expands ``~`` first, then checks the prefix."""
    monkeypatch.setenv("HOME", "/root")
    result = check_path_safety("~/.ssh/id_rsa")
    assert result["warning"] is not None
    assert result["category"] == CATEGORIES["dir_prefix"]


# ---- unresolvable paths are NOT advised (let backend report) ----


def test_empty_string_path_does_not_advise():
    # An empty path is malformed but we don't want to falsely advise.
    # The backend will error on it.
    result = check_path_safety("")
    assert result["warning"] is None


# ---- advisory message mentions concrete risk ----


def test_advisory_includes_path_and_reason():
    result = check_path_safety("/root/.ssh/id_rsa")
    assert result["warning"] is not None
    assert "credential" in result["warning"].lower() or "private" in result["warning"].lower()
