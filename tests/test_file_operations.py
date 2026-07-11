import base64
from unittest.mock import MagicMock

import pytest

from sandbox_mcp.file_operations import FileOperations


@pytest.fixture
def backend():
    return MagicMock()


@pytest.fixture
def fops(backend):
    return FileOperations(backend)


# ---- read ----

def test_read_returns_line_numbered_output(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "42\n", "stderr": "",
    }
    # wc -c, head -c, sed, wc -l all run; last call returns total lines.
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "21\n", "stderr": ""},     # wc -c
        {"exit_code": 0, "output": "line1\nline2\nline3\n", "stderr": ""},  # head -c
        {"exit_code": 0, "output": "line1\nline2\nline3\n", "stderr": ""},  # sed
        {"exit_code": 0, "output": "3\n", "stderr": ""},       # wc -l
    ]
    result = fops.read("/tmp/x.txt", machine="dev")
    assert result["status"] == "ok"
    assert result["path"] == "/tmp/x.txt"
    assert "1|line1" in result["output"]
    assert "3|line3" in result["output"]


def test_read_pagination_offset_and_limit(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "100\n", "stderr": ""},     # wc -c
        {"exit_code": 0, "output": "line2\nline3\n", "stderr": ""},  # head -c
        {"exit_code": 0, "output": "line2\nline3\n", "stderr": ""},  # sed
        {"exit_code": 0, "output": "5\n", "stderr": ""},       # wc -l
    ]
    result = fops.read("/tmp/x.txt", machine="dev", offset=2, limit=2)
    assert result["offset"] == 2
    assert result["limit"] == 2
    # Absolute line numbers from the original file.
    assert "2|line2" in result["output"]
    assert "3|line3" in result["output"]
    assert result["total_lines"] == 5
    assert result["truncated"] is True


def test_read_returns_truncation_hint_at_eof(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "10\n", "stderr": ""},
        {"exit_code": 0, "output": "a\n", "stderr": ""},
        {"exit_code": 0, "output": "a\n", "stderr": ""},
        {"exit_code": 0, "output": "1\n", "stderr": ""},
    ]
    result = fops.read("/tmp/x.txt", machine="dev", offset=1, limit=500)
    assert result["truncated"] is False
    assert "End of file" in result["hint"]


def test_read_not_found_returns_suggestions(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 1, "output": "", "stderr": ""},    # wc -c (file missing)
        {"exit_code": 0, "output": "missing.bak\nother.txt\n", "stderr": ""},  # ls
    ]
    result = fops.read("/tmp/missing.txt", machine="dev")
    assert result["status"] == "not_found"
    assert result["suggestions"] == ["/tmp/missing.bak"]


def test_read_detects_binary(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "1024\n", "stderr": ""},
        {"exit_code": 0, "output": "binary\x00data", "stderr": ""},
    ]
    result = fops.read("/tmp/blob.bin", machine="dev")
    assert result["status"] == "binary"


def test_read_image_returns_image_hint(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "4096\n", "stderr": ""},
    ]
    result = fops.read("/tmp/pic.png", machine="dev")
    assert result["status"] == "image"


# ---- write ----

def test_write_fails_on_invalid_json_content(fops, backend):
    result = fops.write("/tmp/x.json", "{this is not json", machine="dev")
    assert result["status"] == "error"
    assert result["stage"] == "lint_pre_write"
    assert "Refusing" in result["error"]


def test_write_succeeds_on_valid_json(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 1, "output": "", "stderr": ""},  # cat pre-content (none)
        {"exit_code": 0, "output": "", "stderr": ""},  # atomic_write
        {"exit_code": 0, "output": '{"a": 1}\n', "stderr": ""},  # verify
        {"exit_code": 0, "output": "8\n", "stderr": ""},  # wc -c
    ]
    result = fops.write("/tmp/x.json", '{"a": 1}\n', machine="dev")
    assert result["status"] == "ok"
    assert result["bytes_written"] == 8


def test_write_atomic_write_uses_stdin(fops, backend):
    """The atomic-write script must be piped base64 over stdin."""
    backend.exec_oneoff.side_effect = [
        {"exit_code": 1, "output": "", "stderr": ""},  # cat
        {"exit_code": 0, "output": "", "stderr": ""},  # atomic_write
        {"exit_code": 0, "output": "hello\n", "stderr": ""},  # verify
        {"exit_code": 0, "output": "6\n", "stderr": ""},  # wc -c
    ]
    fops.write("/tmp/x.txt", "hello\n", machine="dev")
    write_call = backend.exec_oneoff.call_args_list[1]
    cmd = write_call.args[1]
    stdin = write_call.kwargs.get("stdin_data")
    assert "base64 -d" in cmd
    assert "mktemp" in cmd
    assert "mv -f" in cmd
    assert stdin is not None
    # The stdin payload is the base64 of the literal "hello\n".
    assert base64.b64decode(stdin.strip()).decode() == "hello\n"


def test_write_post_write_verify_detects_mismatch(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 1, "output": "", "stderr": ""},  # cat (none)
        {"exit_code": 0, "output": "", "stderr": ""},  # atomic_write
        {"exit_code": 0, "output": "WRONG\n", "stderr": ""},  # verify mismatch
    ]
    result = fops.write("/tmp/x.txt", "expected\n", machine="dev")
    assert result["status"] == "error"
    assert result["stage"] == "verify"


def test_write_preserves_crlf_when_target_has_it(fops, backend):
    """If the on-disk file uses CRLF, new content is normalized to CRLF."""
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "old\r\nline\r\n", "stderr": ""},  # cat pre
        {"exit_code": 0, "output": "", "stderr": ""},                # atomic_write
        {"exit_code": 0, "output": "old\r\nnew\r\n", "stderr": ""},  # verify
        {"exit_code": 0, "output": "16\n", "stderr": ""},             # wc -c
    ]
    fops.write("/tmp/x.txt", "old\nnew\n", machine="dev")
    # The atomic-write stdin must contain CRLF-encoded new content.
    write_call = backend.exec_oneoff.call_args_list[1]
    decoded = base64.b64decode(write_call.kwargs["stdin_data"].strip())
    assert decoded == b"old\r\nnew\r\n"


def test_write_preserves_bom_when_target_has_it(fops, backend):
    backend.exec_oneoff.side_effect = [
        {"exit_code": 0, "output": "\ufeffhello\n", "stderr": ""},  # cat with BOM
        {"exit_code": 0, "output": "", "stderr": ""},                # atomic_write
        {"exit_code": 0, "output": "\ufeffhello\n", "stderr": ""},   # verify
        {"exit_code": 0, "output": "6\n", "stderr": ""},             # wc -c
    ]
    fops.write("/tmp/x.txt", "hello\n", machine="dev")
    decoded = base64.b64decode(
        backend.exec_oneoff.call_args_list[1].kwargs["stdin_data"].strip())
    assert decoded.startswith(b"\xef\xbb\xbf")


# ---- patch ----

def _patch_sequence(initial_file, post_file):
    """Return a side_effect for patch tests with explicit pre/post file.

    patch_replace calls cat → atomic_write → cat (verify).
    """
    return [
        {"exit_code": 0, "output": initial_file, "stderr": ""},
        {"exit_code": 0, "output": "", "stderr": ""},
        {"exit_code": 0, "output": post_file, "stderr": ""},
    ]


def test_patch_replace_mode_replaces_unique_string(fops, backend):
    initial = "alpha\nbeta\ngamma\n"
    backend.exec_oneoff.side_effect = _patch_sequence(
        initial, "alpha\nBETA\ngamma\n")
    result = fops.patch(mode="replace", machine="dev", path="/tmp/x.txt",
                       old_string="beta", new_string="BETA")
    assert result["status"] == "ok"
    assert result["matches"] == 1


def test_patch_replace_mode_returns_diff(fops, backend):
    initial = "a\nb\nc\n"
    backend.exec_oneoff.side_effect = _patch_sequence(initial, "a\nB\nc\n")
    result = fops.patch(mode="replace", machine="dev", path="/tmp/x.txt",
                       old_string="b", new_string="B")
    assert "diff" in result
    assert "-b" in result["diff"]
    assert "+B" in result["diff"]


def test_patch_replace_mode_rejects_multiple_matches(fops, backend):
    initial = "x\nx\nx\n"
    backend.exec_oneoff.side_effect = _patch_sequence(initial, initial)
    result = fops.patch(mode="replace", machine="dev", path="/tmp/x.txt",
                       old_string="x", new_string="y")
    assert result["status"] == "error"
    assert "Multiple matches" in result["error"]


def test_patch_replace_mode_fuzzy_match(fops, backend):
    initial = "hello world\n"
    backend.exec_oneoff.side_effect = _patch_sequence(initial, initial)
    result = fops.patch(mode="replace", machine="dev", path="/tmp/x.txt",
                       old_string="helo world", new_string="hello world",
                       replace_all=False)
    assert result["status"] == "ok"
    assert result["fuzzy"] is True


def test_patch_replace_mode_normalizes_crlf(fops, backend):
    """A patch sent with LF matches against CRLF on disk and writes CRLF."""
    initial = "alpha\r\nbeta\r\ngamma\r\n"
    expected_after = "alpha\r\nBETA\r\ngamma\r\n"
    backend.exec_oneoff.side_effect = _patch_sequence(
        initial, expected_after)
    result = fops.patch(mode="replace", machine="dev", path="/tmp/x.txt",
                       old_string="beta", new_string="BETA")
    assert result["status"] == "ok"
    decoded = base64.b64decode(
        backend.exec_oneoff.call_args_list[1].kwargs["stdin_data"].strip())
    assert b"\r\n" in decoded


def test_patch_apply_mode(fops, backend):
    backend.exec_oneoff.return_value = {"exit_code": 0, "output": "", "stderr": ""}
    result = fops.patch(mode="patch", machine="dev", patch="--- a/x\n+++ b/x\n")
    assert result["status"] == "ok"


def test_patch_apply_mode_empty(fops, backend):
    result = fops.patch(mode="patch", machine="dev", patch="")
    assert result["status"] == "error"


# ---- search ----

def test_search_content_returns_matching_lines(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0,
        "output": "/tmp/x.txt:3:foo bar\n/tmp/y.txt:7:baz foo\n",
        "stderr": "",
    }
    result = fops.search("foo", machine="dev", search_type="content")
    assert result["status"] == "ok"
    assert len(result["results"]) == 2
    assert result["results"][0]["line"] == 3
    assert result["results"][0]["path"] == "/tmp/x.txt"


def test_search_files_mode_uses_rg(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0,
        "output": "/tmp/a.py\n/tmp/b.py\n",
        "stderr": "",
    }
    result = fops.search("*.py", machine="dev", search_type="files")
    assert result["status"] == "ok"
    assert result["results"] == ["/tmp/a.py", "/tmp/b.py"]
    # The actual command must use rg --files, not find.
    cmd = backend.exec_oneoff.call_args.args[1]
    assert "rg --files" in cmd
    assert "set -o pipefail" in cmd


def test_search_limit_truncates_results(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0,
        "output": "\n".join(f"/tmp/f{i}.py" for i in range(10)) + "\n",
        "stderr": "",
    }
    result = fops.search("*.py", machine="dev", search_type="files", limit=3)
    assert len(result["results"]) == 3


def test_search_content_warns_on_newline_regex(fops, backend):
    backend.exec_oneoff.return_value = {"exit_code": 0, "output": "", "stderr": ""}
    result = fops.search("foo\\nbar", machine="dev", search_type="content")
    assert result.get("warning") is not None
    assert "line-oriented" in result["warning"]


def test_search_separates_rg_diagnostics(fops, backend):
    backend.exec_oneoff.return_value = {
        "exit_code": 0,
        "output": "/tmp/x.txt:3:hit\nrg: permission denied reading /tmp/y.txt\n",
        "stderr": "",
    }
    result = fops.search("hit", machine="dev", search_type="content")
    assert len(result["results"]) == 1
    assert "permission denied" in (result["diagnostics"] or "")


def test_search_rejects_unknown_search_type(fops, backend):
    result = fops.search("foo", machine="dev", search_type="bogus")
    assert result["status"] == "error"


# ---- expand_path ----

def test_expand_tilde_uses_backend_home(fops, backend):
    from sandbox_mcp.file_operations import _expand_path
    backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "/home/dev\n", "stderr": "",
    }
    assert _expand_path("~/x.txt", backend) == "/home/dev/x.txt"
    assert _expand_path("~", backend) == "/home/dev"


def test_expand_path_passthrough_when_no_tilde(fops):
    from sandbox_mcp.file_operations import _expand_path
    assert _expand_path("/tmp/x", fops._backend) == "/tmp/x"
    assert _expand_path("relative", fops._backend) == "relative"


def test_expand_path_lone_tilde(fops):
    from sandbox_mcp.file_operations import _expand_path
    fops._backend.exec_oneoff.return_value = {
        "exit_code": 0, "output": "/home/dev\n", "stderr": "",
    }
    assert _expand_path("~", fops._backend) == "/home/dev"
