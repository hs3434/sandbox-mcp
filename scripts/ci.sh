#!/usr/bin/env bash
# Local mirror of the GitHub Actions "tests" workflow.
#
# Run this before pushing to catch lint / type / test regressions
# locally instead of waiting for CI.  Exits non-zero on the first
# failing step (matching GitHub Actions behaviour).
#
# Usage:
#   ./scripts/ci.sh            # use the project's venv (./.venv or python -m ...)
#   ./scripts/ci.sh --system    # use system-installed ruff/mypy/pytest
#
# If you're running this outside the project venv, the venv tools
# (ruff, mypy, pytest) must be on PATH or this script will fail.

set -euo pipefail

cd "$(dirname "$0")/.."

# Prefer project-local venv (most common case for devs).
if [[ "${1:-}" != "--system" ]]; then
    for venv in .venv venv .; do
        if [[ -x "$venv/bin/ruff" ]]; then
            export PATH="$PWD/$venv/bin:$PATH"
            break
        fi
    done
fi

echo "==> ruff format --check"
ruff format --check .

echo "==> ruff check"
ruff check .

echo "==> mypy src/sandbox_mcp"
mypy src/sandbox_mcp

echo "==> pytest (unit)"
pytest tests/ -v

echo
echo "OK — local CI passed."