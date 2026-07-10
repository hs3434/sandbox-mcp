import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import pytest


@pytest.fixture
def tmp_workdir(tmp_path, monkeypatch):
    """Change to a temp directory for isolated tests."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
