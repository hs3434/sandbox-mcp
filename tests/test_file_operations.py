from unittest.mock import MagicMock

import pytest

from sandbox_mcp.file_operations import FileOperations


@pytest.fixture
def backend():
    return MagicMock()


@pytest.fixture
def fops(backend):
    return FileOperations(backend)


def test_read_returns_line_numbered_output(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "line1\nline2\nline3\n",
    }
    result = fops.read("/tmp/x.txt", target="dev")
    assert result["status"] == "ok"
    assert result["path"] == "/tmp/x.txt"
    assert "1|line1" in result["output"]
    assert "3|line3" in result["output"]


def test_read_pagination_offset_and_limit(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "line2\nline3\n",
    }
    result = fops.read("/tmp/x.txt", target="dev", offset=2, limit=2)
    assert result["offset"] == 2
    assert result["limit"] == 2
    # sed already returns lines starting at the requested offset, so
    # line numbers are absolute from the original file.
    assert "2|line2" in result["output"]
    assert "3|line3" in result["output"]
    assert "line1" not in result["output"]


def test_read_not_found_returns_suggestions(fops, backend):
    backend.exec_oneoff.return_value = {"exit_code": 1, "output": ""}
    backend.suggest_paths.return_value = ["/tmp/x.txt", "/tmp/x.txt.bak"]
    result = fops.read("/tmp/missing.txt", target="dev")
    assert result["status"] == "not_found"
    assert result["suggestions"] == ["/tmp/x.txt", "/tmp/x.txt.bak"]


def test_read_binary_returns_error(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "binary\x00data",
    }
    result = fops.read("/tmp/blob.bin", target="dev")
    assert result["status"] == "binary"
    assert "error" in result


def test_write_creates_parent_dirs_and_writes(fops, backend):
    backend.exec_oneoff.return_value = {"exit_code": 0, "output": ""}
    result = fops.write("/tmp/new/dir/x.txt", "hello", target="dev")
    assert result["status"] == "ok"
    cmds = [c.args[1] for c in backend.exec_oneoff.call_args_list]
    assert any("mkdir -p" in cmd for cmd in cmds)
    assert any("/tmp/new/dir/x.txt" in cmd for cmd in cmds)


def test_write_runs_syntax_check_when_extension_known(fops, backend):
    backend.exec_oneoff.return_value = {"exit_code": 0, "output": ""}
    fops.write("/tmp/x.py", "print(1)\n", target="dev")
    cmds = [c.args[1] for c in backend.exec_oneoff.call_args_list]
    assert any("python -m py_compile" in cmd for cmd in cmds)


def test_patch_replace_mode_replaces_unique_string(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "alpha\nbeta\ngamma\n"},
        {"exit_code": 0, "output": ""},
    ]
    result = fops.patch(mode="replace", target="dev", path="/tmp/x.txt",
                       old_string="beta", new_string="BETA")
    assert result["status"] == "ok"
    assert result["matches"] == 1


def test_patch_replace_mode_returns_diff(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "a\nb\nc\n"},
        {"exit_code": 0, "output": ""},
    ]
    result = fops.patch(mode="replace", target="dev", path="/tmp/x.txt",
                       old_string="b", new_string="B")
    assert "diff" in result
    assert "-b" in result["diff"]
    assert "+B" in result["diff"]


def test_patch_replace_mode_rejects_multiple_matches(fops, backend):
    backend.exec_oneoff.return_value = {"exit_code": 0, "output": "x\nx\nx\n"}
    result = fops.patch(mode="replace", target="dev", path="/tmp/x.txt",
                       old_string="x", new_string="y")
    assert result["status"] == "error"
    assert "Multiple matches" in result["error"]


def test_patch_replace_mode_fuzzy_match(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "hello world\n"},
        {"exit_code": 0, "output": ""},
    ]
    result = fops.patch(mode="replace", target="dev", path="/tmp/x.txt",
                       old_string="helo world", new_string="hello world",
                       replace_all=False)
    assert result["status"] == "ok"
    assert result["fuzzy"] is True


def test_search_content_returns_matching_lines(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0,
        "output": "/tmp/x.txt:3:foo bar\n/tmp/y.txt:7:baz foo\n",
    }
    result = fops.search("foo", target="dev", search_type="content")
    assert result["status"] == "ok"
    assert len(result["results"]) == 2
    assert result["results"][0]["line"] == 3
    assert result["results"][0]["path"] == "/tmp/x.txt"


def test_search_files_mode_uses_glob(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "/tmp/a.py\n/tmp/b.py\n",
    }
    result = fops.search("*.py", target="dev", search_type="files")
    assert result["status"] == "ok"
    assert result["results"] == ["/tmp/a.py", "/tmp/b.py"]


def test_search_limit_truncates_results(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0,
        "output": "\n".join(f"/tmp/f{i}.py" for i in range(10)) + "\n",
    }
    result = fops.search("*.py", target="dev", search_type="files", limit=3)
    assert len(result["results"]) == 3
