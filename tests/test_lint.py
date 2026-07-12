"""Lint and format checks that run as part of ``pytest``.

These tests spawn ``ruff`` as a subprocess so a single ``pytest`` run
catches the same things CI does.  Run individually with:

    pytest tests/test_lint.py -v
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ruff_available() -> bool:
    return shutil.which("ruff") is not None


pytestmark = pytest.mark.skipif(not _ruff_available(), reason="ruff not on PATH")


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
