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

"""Lint and format checks that run as part of ``pytest``.

These tests spawn ``ruff`` as a subprocess so a single ``pytest`` run
catches the same things CI does.  Run individually with:

    pytest tests/test_lint.py -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_ruff_format_check():
    """``ruff format --check .`` must pass.

    On failure the diff is included so the developer can see what to
    fix without leaving the test runner.  A 60s timeout guards against
    ruff hanging in CI.
    """
    result = subprocess.run(
        ["ruff", "format", "--check", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"ruff format --check failed.  Run `ruff format .` to fix.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def test_ruff_check():
    """``ruff check .`` must pass (no lint errors).

    Auto-fixable issues can be cleared with ``ruff check --fix .``.
    A 60s timeout guards against ruff hanging in CI.
    """
    result = subprocess.run(
        ["ruff", "check", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"ruff check failed.  Run `ruff check --fix .` to fix.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
