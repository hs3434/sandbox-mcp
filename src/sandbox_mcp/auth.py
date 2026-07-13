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

"""Bearer-token authentication for the HTTP transport.

Token source
------------
Tokens are read from a file on disk (one per line, ``#``-comments OK,
blank lines ignored).  Resolution order (highest first):

1. ``$SANDBOX_MCP_SERVER_AUTH_TOKENS_FILE``
2. ``[server] auth_tokens_file`` in ``config.toml``
3. ``~/.sandbox-mcp/auth_tokens`` (default)

The file MUST be mode ``0600`` (owner read/write only).  Anything more
permissive is **rejected at load time** — sandbox-mcp fails closed
rather than serve requests with a token file that another user on the
host could read.

Auto-generation
---------------
If the token file is missing or empty AND
``[server] auto_generate_if_empty = true``, an ephemeral token is
generated at startup and printed to stderr.  Operators must capture
it before the first client connects.

Why a file, not an env var
--------------------------
Env vars leak into process listings (``/proc/<pid>/environ``), shell
history, and crash dumps.  Tokens belong on disk in a 0600 file, like
SSH private keys.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

from sandbox_mcp.config import load as _load_config

DEFAULT_TOKENS_FILE = "~/.sandbox-mcp/auth_tokens"
_ENV_OVERRIDE = "SANDBOX_MCP_SERVER_AUTH_TOKENS_FILE"

# Minimum token length.  32 chars ≈ 190 bits at base64url entropy — short
# enough for operators to type once, long enough to resist brute force.
_MIN_TOKEN_LEN = 32

# Mode bits that, if set, mean the file is readable by someone other than
# the owner.  We refuse to load such a file (fail-closed).
_WORLD_READABLE_MASK = 0o077


def resolve_tokens_file() -> Path:
    """Return the resolved path of the auth-tokens file.

    Precedence: env var > config.toml > default path.  The path is
    ``~``-expanded but NOT ``resolve()``d — the file may not exist yet,
    which is a valid state (``auto_generate_if_empty`` may handle it).
    """
    raw = os.environ.get(_ENV_OVERRIDE)
    if raw:
        return Path(raw).expanduser()
    cfg_path = _load_config().server.auth_tokens_file
    if cfg_path:
        return Path(cfg_path).expanduser()
    return Path(DEFAULT_TOKENS_FILE).expanduser()


def load_auth_tokens(tokens_file: Path | None = None) -> tuple[str, ...]:
    """Read bearer tokens from ``tokens_file`` (default: resolved path).

    Returns an empty tuple if the file is missing.  Warns (and returns
    empty) if the file is too permissive.  Skips blank lines and lines
    starting with ``#``.
    """
    path = tokens_file or resolve_tokens_file()
    if not path.is_file():
        return ()

    mode = path.stat().st_mode & 0o777
    if mode & _WORLD_READABLE_MASK:
        warnings.warn(
            f"auth_tokens file {path} mode {oct(mode)} is too permissive; "
            f"expected 0600 (owner read/write only).  Refusing to read.",
            stacklevel=2,
        )
        return ()

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        warnings.warn(f"failed to read auth_tokens file {path}: {e}", stacklevel=2)
        return ()

    tokens = tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    short = [t for t in tokens if len(t) < _MIN_TOKEN_LEN]
    if short:
        warnings.warn(
            f"auth_tokens file {path} contains {len(short)} token(s) shorter "
            f"than {_MIN_TOKEN_LEN} chars; consider regenerating with "
            f"`secrets.token_urlsafe(32)`.",
            stacklevel=2,
        )

    return tokens


def generate_ephemeral_token() -> str:
    """Generate a fresh random token (URL-safe, ~256 bits)."""
    import secrets

    return secrets.token_urlsafe(32)
