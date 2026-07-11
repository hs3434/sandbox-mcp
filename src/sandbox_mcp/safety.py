"""Path safety advisories for sandbox-mcp file operations.

This module mirrors :mod:`hermes-agent.agent.file_safety` (denylist scope
and reasoning) but returns **advisories**, not errors. The denylist itself
is the same: ``/etc/shadow`` and friends, ``~/.ssh``, ``~/.aws``, ``.env``
files, etc. The behaviour diverges on the response:

* hermes-agent **raises** because it serves a human-monitored agent;
  blocking a tool call surfaces the violation cleanly.
* sandbox-mcp **returns a warning** because it serves autonomous agents
  (its own "terminal" tool is what the agent drives to make decisions).
  Hard blocking would defeat the autonomy; surfacing the risk and
  letting the agent decide is more honest.

Both designs are honest about being **defense-in-depth, not a security
boundary**: the agent can still call ``sandbox_shell_exec`` with
``cat /etc/shadow`` and read it. The advisory exists to give the model
a clear signal that "this path looks risky" so it can stop or proceed
explicitly, and so the audit log records the attempt.
"""

from __future__ import annotations

import os

# Exact-file system sensitive paths. Mirrors
# ``agent.file_safety.build_write_denied_paths`` minus Hermes-specific
# entries (mcp-sandbox has no HERMES_HOME concept).
_BLOCKED_EXACT_PATHS: frozenset[str] = frozenset({
    "/etc/shadow",
    "/etc/passwd",
    "/etc/sudoers",
    "/etc/gshadow",
})


# Path prefixes whose presence in the target's path means "this is a
# credential / private-key / config directory". Symlink-resolved paths
# are compared to avoid ``../.ssh``-style bypass.
_BLOCKED_PATH_PREFIXES: tuple[str, ...] = (
    "/etc/sudoers.d",
    "/etc/systemd",
    "/root/.ssh",
    "/root/.aws",
    "/root/.azure",
    "/root/.gnupg",
    "/root/.kube",
    "/root/.docker",
    "/root/.netrc",
    "/root/.pgpass",
    "/root/.pypirc",
    "/root/.npmrc",
    "/root/.git-credentials",
    "/root/.config/gh",
    "/root/.config/gcloud",
    # /home/... (covers any non-root user's homedir)
    "/home/.ssh",
    "/home/.aws",
    "/home/.gnupg",
    "/home/.kube",
    # /home/<user>/.ssh etc. (covers any specific user's homedir)
    "/home/user/.ssh",
    "/home/user/.aws",
    "/home/user/.gnupg",
    "/home/user/.kube",
    "/home/deploy/.ssh",
    "/home/deploy/.aws",
)


# Specific filenames commonly holding secrets. The basename check fires
# regardless of parent directory (matches hermes' project-env guard,
# which fires for any path ending in .env, .env.local, etc.).
_BLOCKED_BASENAMES: frozenset[str] = frozenset({
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    ".env.staging",
    ".envrc",
    # Credential / private-key filenames inside .ssh/ — checked here too
    # so a file at e.g. /tmp/oddity/id_rsa still triggers.
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
})


# Public for tests / advanced callers.
CATEGORIES = {
    "exact_path": "system exact-file denylist (/etc/shadow, /etc/passwd, ...)",
    "dir_prefix": "credential directory (e.g. ~/.ssh, ~/.aws)",
    "basename": "secret-bearing filename (.env, id_rsa)",
}


def _normalize(path: str) -> str:
    """Resolve to absolute, follow symlinks, expand user home.

    Defense against ``../etc/shadow`` style bypasses.
    """
    return os.path.realpath(os.path.expanduser(path))


def check_path_safety(path: str) -> dict:
    """Return a non-blocking advisory for ``path``.

    Returns ``{"warning": None, "category": None}`` when the path looks
    safe, or ``{"warning": "<message>", "category": "<why>"}`` when the
    path matches one of the denylist entries. The caller surfaces the
    warning to the agent and to the audit log but does not block.
    """
    try:
        resolved = _normalize(path)
    except (OSError, RuntimeError, ValueError):
        # Unresolvable paths (e.g. ENAMETOOLONG, ENOENT in odd states)
        # are not denylisted. Better to let the backend surface its own
        # error than to guess.
        return {"warning": None, "category": None}

    if resolved in _BLOCKED_EXACT_PATHS:
        return {
            "warning": (
                f"Path '{path}' (resolves to '{resolved}') is a "
                "system credential store. Writing here typically "
                "overwrites security-sensitive state (e.g. /etc/shadow). "
                "If the agent intended this, proceed explicitly; otherwise "
                "use a different path."
            ),
            "category": CATEGORIES["exact_path"],
        }

    for prefix in _BLOCKED_PATH_PREFIXES:
        if resolved == prefix or resolved.startswith(prefix + os.sep):
            return {
                "warning": (
                    f"Path '{path}' (resolves to '{resolved}') sits under "
                    f"'{prefix}', which commonly contains private keys, "
                    "credentials, or service config. If the agent intended "
                    "this, proceed explicitly; otherwise use a different path."
                ),
                "category": CATEGORIES["dir_prefix"],
            }

    basename = os.path.basename(resolved).lower()
    if basename in _BLOCKED_BASENAMES:
        if basename.startswith(".env") or basename == ".envrc":
            category = CATEGORIES["basename"]
            context = "environment file"
            detail = (
                "Project-local environment files commonly contain API "
                "keys, database passwords, and other credentials. If the "
                "agent intended to write here, proceed explicitly; "
                "otherwise read .env.example for the documented shape."
            )
        else:
            category = CATEGORIES["basename"]
            context = "private-key file"
            detail = (
                "Private-key filenames (id_rsa, id_dsa, id_ecdsa, "
                "id_ed25519) are credential material. If the agent intended "
                "to write a key here, proceed explicitly; otherwise use a "
                "different filename."
            )
        return {
            "warning": (
                f"Path '{path}' (resolves to '{resolved}') is a "
                f"{context}. {detail}"
            ),
            "category": category,
        }

    return {"warning": None, "category": None}


def is_read_denied(path: str) -> bool:
    """True if a read at ``path`` should trigger the advisory."""
    return check_path_safety(path)["warning"] is not None


def is_write_denied(path: str) -> bool:
    """True if a write at ``path`` should trigger the advisory.

    Same predicate as :func:`is_read_denied` — a path that warrants an
    advisory for reading also warrants one for writing.
    """
    return is_read_denied(path)
