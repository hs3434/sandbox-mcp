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

"""File operations executed via a backend's one-off shell commands.

Design notes (see audit/permissions section of the design spec):

* All shell arguments are quoted via :func:`shlex.quote`.
* File contents ride through **stdin over a base64 pipe** rather than a
  heredoc, so arbitrary user content cannot collide with a delimiter
  marker.
* Writes are **atomic**: content is staged into a temp file in the same
  directory and then ``mv -f`` over the target. A crash between the two
  steps leaves the original file intact.
* In-process linters (Python ``ast``, ``json``, optional ``yaml``/``toml``)
  validate candidate content before any bytes touch disk for structured
  formats where "doesn't parse" always means "corrupt".
* Post-write verification re-reads the file and confirms the intended
  bytes actually landed. Catches backend-specific persistence failures
  (truncated pipe, race with another process) that would otherwise
  return success-with-unchanged-disk.
* Line endings and UTF-8 BOM are preserved when the on-disk file had
  them, so round-tripping a CRLF Windows file through the agent does
  not silently flip it to LF.
* Read sizes are bounded so a 100 MB log does not blow the model's
  context window in one shot.
"""

from __future__ import annotations

import base64
import difflib
import json as _json
import os
import re
import shlex

from sandbox_mcp.config import load as _load_config
from sandbox_mcp.safety import check_path_safety

LINE_FMT = "{n}|{line}"


def _files_cfg():
    return _load_config().files


# Extensions where we have a fast in-process linter and a failed parse
# always means corruption (not a style nit). These get a fail-closed
# pre-write gate. Python is intentionally NOT here: the codebase uses
# ``*.py`` for arbitrary stand-in fixtures (see hermes-agent's
# ``_FAIL_CLOSED_INPROC_EXTS`` rationale).
_FAIL_CLOSED_INPROC_EXTS = frozenset({".json", ".yaml", ".yml", ".toml"})


def _expand_path(path: str, backend) -> str:
    """Expand ``~`` and ``~user`` before quoting for the shell.

    Single-quoted paths in bash don't expand ``~`` so we resolve on the
    host before sending. ``~user`` is expanded only when ``user``
    matches a safe character class (no shell injection via ``~$(rm)``).
    """
    if not path or "~" not in path:
        return path
    # Use the backend's echo to resolve $HOME / ~user.
    r = backend.exec_oneoff("_resolve_path", "echo $HOME")
    home = (r.get("output") or "").strip()
    if path == "~":
        return home
    if path.startswith("~/") and home:
        return home + path[1:]
    if path.startswith("~"):
        rest = path[1:]
        slash = rest.find("/")
        username = rest[:slash] if slash >= 0 else rest
        if username and re.fullmatch(r"[a-zA-Z0-9._-]+", username):
            r2 = backend.exec_oneoff("_resolve_path", f"echo ~{username}")
            user_home = (r2.get("output") or "").strip()
            if user_home and user_home != f"~{username}":
                suffix = path[1 + len(username) :]
                return user_home + suffix
    return path


def _detect_line_ending(text: str) -> str:
    """Return the dominant line ending in ``text``: ``\\r\\n`` or ``\\n``."""
    return "\r\n" if "\r\n" in text else "\n"


def _normalize_line_endings(text: str, ending: str) -> str:
    """Convert all line endings in ``text`` to ``ending``."""
    if ending == "\n":
        return text.replace("\r\n", "\n")
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def _has_bom(text: str) -> bool:
    return text.startswith("\ufeff")


def _strip_bom(text: str) -> tuple[str, bool]:
    if text.startswith("\ufeff"):
        return text[1:], True
    return text, False


def _strip_terminal_fence_leaks(text: str) -> str:
    """Strip terminal escape sequences that leak into captured stdout."""
    # Remove CSI sequences, OSC sequences, simple color codes.
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07", "", text)


# ---- In-process linters ----------------------------------------------------


def _lint_python_inproc(content: str) -> tuple[bool, str]:
    import ast

    try:
        ast.parse(content)
        return True, ""
    except SyntaxError as e:
        loc = f" (line {e.lineno}, column {e.offset})" if e.lineno else ""
        return False, f"{type(e).__name__}: {e.msg}{loc}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _lint_json_inproc(content: str) -> tuple[bool, str]:
    try:
        _json.loads(content)
        return True, ""
    except _json.JSONDecodeError as e:
        return False, f"JSONDecodeError: {e.msg} (line {e.lineno}, column {e.colno})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _lint_yaml_inproc(content: str) -> tuple[bool, str]:
    try:
        import yaml
    except ImportError:
        return True, "__SKIP__"
    try:
        for _ in yaml.parse(content):
            pass
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _lint_toml_inproc(content: str) -> tuple[bool, str]:
    try:
        import tomllib  # py3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return True, "__SKIP__"
    try:
        tomllib.loads(content)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


_LINTERS_INPROC = {
    ".py": _lint_python_inproc,
    ".json": _lint_json_inproc,
    ".yaml": _lint_yaml_inproc,
    ".yml": _lint_yaml_inproc,
    ".toml": _lint_toml_inproc,
}


# ---- Binary detection ------------------------------------------------------


def _is_binary_file(path: str, sample: bytes) -> bool:
    """Combined extension + content heuristic, matching opencode's approach."""
    ext = os.path.splitext(path)[1].lower()
    binary_exts = {
        ".zip",
        ".tar",
        ".gz",
        ".exe",
        ".dll",
        ".so",
        ".class",
        ".jar",
        ".war",
        ".7z",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".bin",
        ".dat",
        ".obj",
        ".o",
        ".a",
        ".lib",
        ".wasm",
        ".pyc",
        ".pyo",
    }
    if ext in binary_exts:
        return True
    if not sample:
        return False
    # >30% non-printable bytes (excluding common whitespace) means binary.
    non_printable = sum(1 for b in sample if b == 0 or (b < 9 or (b > 13 and b < 32)))
    return non_printable / len(sample) > 0.30


def _is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}


# ---- Atomic write ---------------------------------------------------------


def _atomic_write(backend, machine: str, path: str, content: bytes) -> dict:
    """Write content to the target file via the backend's write_file hook.

    For Docker, the backend uses ``put_archive`` (no shell quoting, no
    ARG_MAX). For SSH, the backend pipes base64 over stdin. The backend
    is responsible for atomicity (staging a temp file then ``mv``-ing
    into place).
    """
    return backend.write_file(machine, path, content)


# ---- Unified diff ----------------------------------------------------------


def _unified_diff(old: str, new: str, filename: str) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )


# ---- Search helpers -------------------------------------------------------

_REGEX_NEWLINE_RE = re.compile(r"(?<!\\)(?:\\\\)*\\n")


def _pattern_has_regex_newline(pattern: str) -> bool:
    """Detect ``\\n`` in a regex pattern that won't match across lines."""
    return "\n" in pattern or bool(_REGEX_NEWLINE_RE.search(pattern))


def _split_tool_diagnostics(output: str) -> tuple[str, str]:
    """Separate ripgrep/grep stderr diagnostics from match output.

    ripgrep diagnostics look like ``rg: <path>: <message>``. They must
    not be parsed as matches and they ARE the message on a hard error
    (exit code 2).
    """
    diagnostics: list[str] = []
    payload: list[str] = []
    for line in output.splitlines():
        if line.startswith("rg:") or line.startswith("grep:"):
            diagnostics.append(line)
        else:
            payload.append(line)
    return "\n".join(diagnostics), "\n".join(payload)


# ---- Main class ------------------------------------------------------------


class FileOperations:
    """Read/write/patch/search via backend.exec_oneoff."""

    def __init__(self, backend):
        self._backend = backend

    # ---- read ----

    def read(self, path: str, machine: str, offset: int = 1, limit: int | None = None) -> dict:
        path = _expand_path(path, self._backend)
        advisory = check_path_safety(path)
        cfg = _files_cfg()
        offset = max(1, int(offset))
        if limit is None:
            limit = cfg.default_read_limit
        limit = max(1, min(int(limit), cfg.max_read_limit))

        q_path = shlex.quote(path)
        # File size guard — wc -c is POSIX, works on Linux + macOS.
        size_result = self._backend.exec_oneoff(machine, f"wc -c < {q_path} 2>/dev/null")
        if size_result.get("exit_code") not in (0, None):
            return self._suggest_similar_files(path, machine)
        try:
            file_size = int(size_result.get("output", "").strip())
        except ValueError:
            file_size = 0

        if file_size > cfg.max_file_size:
            return {
                "status": "too_large",
                "file_size": file_size,
                "max_file_size": cfg.max_file_size,
                "hint": (
                    f"File is {file_size} bytes; exceeds the configured "
                    f"max_file_size of {cfg.max_file_size}. "
                    "Use the shell tool to inspect it directly."
                ),
            }

        # Images are out of scope for the text-mode read; caller should
        # download them via the shell tool.
        if _is_image(path):
            return {"status": "image", "hint": "Image file. Use shell to inspect (e.g. `file`)."}

        # Sample first 4 KB for binary detection.
        sample_result = self._backend.exec_oneoff(machine, f"head -c 4096 {q_path} 2>/dev/null")
        sample = (sample_result.get("output") or "").encode("utf-8", errors="replace")
        if _is_binary_file(path, sample):
            return {
                "status": "binary",
                "file_size": file_size,
                "error": "Binary file cannot be displayed as text.",
            }

        end_line = offset + limit - 1
        read_cmd = f"sed -n {offset},{end_line}p {q_path}"
        result = self._backend.exec_oneoff(machine, read_cmd)
        if result.get("exit_code") not in (0, None):
            return self._suggest_similar_files(path, machine)
        text, _bom = _strip_bom(_strip_terminal_fence_leaks(result.get("output", "") or ""))

        wc_result = self._backend.exec_oneoff(machine, f"wc -l < {q_path}")
        try:
            total_lines = int(wc_result.get("output", "").strip())
        except ValueError:
            total_lines = 0

        truncated = total_lines > end_line
        hint = None
        if truncated:
            hint = (
                f"Use offset={end_line + 1} to continue reading "
                f"(showing {offset}-{end_line} of {total_lines} lines)"
            )
        elif total_lines > 0 and end_line >= total_lines:
            hint = f"(End of file - total {total_lines} lines)"

        lines = text.split("\n")
        max_line_len = cfg.max_line_length
        numbered = [
            (
                LINE_FMT.format(n=offset + i, line=line[:max_line_len])
                + ("... [truncated]" if len(line) > max_line_len else "")
            )
            for i, line in enumerate(lines)
        ]
        result = {
            "status": "ok",
            "path": path,
            "offset": offset,
            "limit": limit,
            "total_lines": total_lines,
            "truncated": truncated,
            "hint": hint,
            "output": "\n".join(numbered) + ("\n" if numbered else ""),
        }
        if advisory["warning"]:
            result["warning"] = advisory["warning"]
            result["safety_category"] = advisory["category"]
        return result

    def _suggest_similar_files(self, path: str, machine: str) -> dict:
        """Return a not_found result with up to 5 similar files."""
        dirname = os.path.dirname(path) or "."
        basename = os.path.basename(path)
        q_dir = shlex.quote(dirname)
        ls_cmd = f"ls -1 {q_dir} 2>/dev/null | head -50"
        ls_result = self._backend.exec_oneoff(machine, ls_cmd)
        candidates: list[str] = []
        if ls_result.get("exit_code") == 0 and ls_result.get("output"):
            lower_q = basename.lower()
            for entry in (ls_result["output"] or "").splitlines():
                entry = entry.strip()
                if not entry:
                    continue
                le = entry.lower()
                if lower_q and (lower_q in le or le in lower_q or le.startswith(lower_q[:3])):
                    candidates.append(os.path.join(dirname, entry))
            candidates = candidates[:5]
        return {"status": "not_found", "path": path, "suggestions": candidates}

    # ---- write ----

    def write(self, path: str, content: str, machine: str) -> dict:
        path = _expand_path(path, self._backend)
        advisory = check_path_safety(path)
        ext = os.path.splitext(path)[1].lower()

        # Pre-write fail-closed gate for structured formats. The shim
        # for line-ending / BOM preservation has not run yet — lint the
        # raw ``content`` so a malformed BOM-laden file does not produce a
        # spurious parse error.
        linter = _LINTERS_INPROC.get(ext) if ext in _FAIL_CLOSED_INPROC_EXTS else None
        if linter is not None:
            ok, err = linter(content)
            if not ok and err != "__SKIP__":
                return {
                    "status": "error",
                    "stage": "lint_pre_write",
                    "error": (
                        f"Refusing to write '{path}': candidate content "
                        f"fails {ext} syntax validation ({err}). "
                        "File was NOT created or modified."
                    ),
                }

        # Probe on-disk file (best-effort) so we can preserve its line
        # ending and BOM. ``cat`` may fail for non-existent files; that
        # is fine — no existing file means no preservation needed.
        cat_result = self._backend.exec_oneoff(machine, f"cat {shlex.quote(path)} 2>/dev/null")
        pre_content = ""
        has_pre_bom = False
        if cat_result.get("exit_code") == 0 and cat_result.get("output"):
            pre_content, has_pre_bom = _strip_bom(cat_result["output"])

        original_ending = _detect_line_ending(pre_content) if pre_content else "\n"
        if original_ending == "\r\n":
            content_to_write = _normalize_line_endings(content, "\r\n")
        else:
            content_to_write = content

        # Restore BOM if the original file had one.
        if has_pre_bom and not content_to_write.startswith("\ufeff"):
            content_to_write = "\ufeff" + content_to_write

        # Backend.write_file handles atomicity and transport (Docker's
        # put_archive, or base64-over-stdin for SSH).
        write_bytes = content_to_write.encode("utf-8")
        write_result = _atomic_write(self._backend, machine, path, write_bytes)
        if write_result.get("status") != "ok":
            return {
                "status": "error",
                "stage": "write",
                "error": (
                    write_result.get("error")
                    or write_result.get("stderr")
                    or write_result.get("output")
                    or "atomic write failed"
                ),
            }

        # Post-write verification: re-read and compare to intended.
        verify = self._backend.exec_oneoff(machine, f"cat {shlex.quote(path)} 2>/dev/null")
        if verify.get("exit_code") not in (0, None):
            return {
                "status": "error",
                "stage": "verify",
                "error": "could not re-read file after write",
            }
        actual = verify.get("output") or ""
        if _strip_bom(actual)[0] != content_to_write:
            return {
                "status": "error",
                "stage": "verify",
                "error": ("on-disk content differs from intended write; the patch did not persist"),
            }

        # Post-write lint summary for structured formats (non-blocking).
        lint_summary = None
        if linter is not None:
            ok, err = linter(content)
            lint_summary = {"ok": ok, "error": err if err != "__SKIP__" else None}

        # bytes written (returned by backend or fall back to content length)
        bytes_written = write_result.get("bytes_written", len(write_bytes))

        result = {
            "status": "ok",
            "path": path,
            "bytes_written": bytes_written,
            "lint": lint_summary,
        }
        if advisory["warning"]:
            result["warning"] = advisory["warning"]
            result["safety_category"] = advisory["category"]
        return result

    # ---- patch ----

    def patch(
        self,
        mode: str,
        machine: str,
        path: str = "",
        old_string: str = "",
        new_string: str = "",
        replace_all: bool = False,
        patch: str = "",
    ) -> dict:
        if mode == "replace":
            return self._patch_replace(machine, path, old_string, new_string, replace_all)
        if mode == "patch":
            return self._patch_apply(machine, patch)
        return {"status": "error", "error": f"Unknown patch mode: {mode}"}

    def _patch_replace(
        self, machine: str, path: str, old_string: str, new_string: str, replace_all: bool
    ) -> dict:
        path = _expand_path(path, self._backend)
        advisory = check_path_safety(path)
        result = self._backend.exec_oneoff(machine, f"cat {shlex.quote(path)}")
        if result.get("exit_code") not in (0, None):
            return {"status": "not_found", "path": path}
        original, original_bom = _strip_bom(result.get("output") or "")
        ending = _detect_line_ending(original) if original else "\n"
        old_norm = _normalize_line_endings(old_string, ending) if old_string else old_string
        new_norm = _normalize_line_endings(new_string, ending) if new_string else new_string

        count = original.count(old_norm)
        fuzzy = False
        if count == 0 and "\n" not in old_string:
            match = difflib.get_close_matches(old_norm, original.splitlines(), n=1, cutoff=0.6)
            if match:
                old_norm = match[0]
                count = original.count(old_norm)
                fuzzy = True
        if count == 0:
            return {"status": "error", "error": "old_string not found"}
        if count > 1 and not replace_all:
            return {"status": "error", "error": f"Multiple matches ({count}); set replace_all=true"}
        replaced = original.replace(old_norm, new_norm)
        diff = _unified_diff(original, replaced, path)

        # Restore BOM if the original had one and preserve the detected
        # line ending.
        full_content = ("\ufeff" if original_bom else "") + replaced
        if ending == "\r\n" and not full_content.endswith("\r\n"):
            full_content += "\r\n"
        elif ending == "\n" and not full_content.endswith("\n"):
            full_content += "\n"

        write_bytes = full_content.encode("utf-8")
        write_result = _atomic_write(self._backend, machine, path, write_bytes)
        if write_result.get("status") != "ok":
            return {
                "status": "error",
                "stage": "write",
                "error": (
                    write_result.get("error") or write_result.get("output") or "patch write failed"
                ),
            }

        # Post-write verification.
        verify = self._backend.exec_oneoff(machine, f"cat {shlex.quote(path)}")
        if verify.get("exit_code") not in (0, None):
            return {"status": "error", "error": "post-patch re-read failed"}
        actual = verify.get("output") or ""
        expected = full_content
        if ending == "\r\n":
            expected = expected.replace("\r\n", "\n").replace("\n", "\r\n")
        if _strip_bom(actual)[0] != expected:
            return {"status": "error", "error": "patch did not persist"}

        result = {"status": "ok", "path": path, "matches": count, "fuzzy": fuzzy, "diff": diff}
        if advisory["warning"]:
            result["warning"] = advisory["warning"]
            result["safety_category"] = advisory["category"]
        return result

    def _patch_apply(self, machine: str, patch_text: str) -> dict:
        if not patch_text.strip():
            return {"status": "error", "error": "patch is empty"}
        encoded = base64.b64encode(patch_text.encode("utf-8")).decode("ascii")
        result = self._backend.exec_oneoff(
            machine, f"echo {shlex.quote(encoded)} | base64 -d | patch -p0"
        )
        if result.get("exit_code") not in (0, None):
            return {"status": "error", "error": result.get("stderr") or "patch failed"}
        return {"status": "ok"}

    # ---- search ----

    def search(
        self,
        pattern: str,
        machine: str,
        search_type: str = "content",
        path: str = ".",
        file_glob: str = "",
        limit: int | None = None,
        offset: int = 0,
        output_mode: str = "content",
        context: int = 0,
    ) -> dict:
        if limit is None:
            limit = _files_cfg().default_search_limit
        # Validate search_type up-front; cheaper than shipping to backend.
        if search_type not in ("content", "files"):
            return {"status": "error", "error": f"Unknown search_type: {search_type}"}

        path = _expand_path(path, self._backend)
        offset = max(0, int(offset))
        limit = max(1, int(limit))

        if search_type == "files":
            return self._search_files(pattern, machine, path, limit, offset)

        return self._search_content(
            pattern,
            machine,
            path,
            file_glob,
            limit,
            offset,
            output_mode,
            context,
        )

    def _search_files(self, pattern: str, machine: str, path: str, limit: int, offset: int) -> dict:
        """Glob-style file search via ripgrep --files with mtime sort."""
        glob_pattern = (
            f"*{pattern}" if "/" not in pattern and not pattern.startswith("*") else pattern
        )
        fetch = limit + offset
        cmd = (
            f"set -o pipefail; "
            f"rg --files --sortr=modified -g {shlex.quote(glob_pattern)} "
            f"{shlex.quote(path)} 2>/dev/null | head -n {fetch}"
        )
        result = self._backend.exec_oneoff(machine, cmd, timeout=60)
        diagnostics, payload = _split_tool_diagnostics(result.get("output") or "")
        files = [f for f in payload.splitlines() if f]
        truncated = len(files) >= fetch or bool(diagnostics)
        return {
            "status": "ok",
            "type": "files",
            "results": files[offset : offset + limit],
            "diagnostics": diagnostics or None,
            "truncated": truncated,
        }

    def _search_content(
        self,
        pattern: str,
        machine: str,
        path: str,
        file_glob: str,
        limit: int,
        offset: int,
        output_mode: str,
        context: int,
    ) -> dict:
        """ripgrep-based content search with diagnostics separation."""
        q_pattern = shlex.quote(pattern)
        q_path = shlex.quote(path)
        cmd_parts = ["set -o pipefail; rg", "--line-number", "--no-heading", "--with-filename"]
        if context > 0:
            cmd_parts += ["-C", str(context)]
        if file_glob:
            cmd_parts += ["--glob", shlex.quote(file_glob)]
        if output_mode == "files_only":
            cmd_parts.append("-l")
        elif output_mode == "count":
            cmd_parts.append("-c")
        cmd_parts += [q_pattern, q_path, "|", "head", "-n", str(limit + offset)]
        result = self._backend.exec_oneoff(machine, " ".join(cmd_parts), timeout=60)
        diagnostics, payload = _split_tool_diagnostics(result.get("output") or "")
        if result.get("exit_code") == 2 and not payload.strip():
            return {
                "status": "error",
                "error": diagnostics.strip() or "search failed",
                "results": [],
            }

        if output_mode == "files_only":
            all_files = [f for f in payload.splitlines() if f]
            return {
                "status": "ok",
                "type": "files",
                "results": all_files[offset : offset + limit],
                "diagnostics": diagnostics or None,
            }
        if output_mode == "count":
            counts: dict[str, int] = {}
            for line in payload.splitlines():
                if not line.strip():
                    continue
                if ":" in line:
                    path_part, _, num = line.rpartition(":")
                    try:
                        counts[path_part] = int(num)
                    except ValueError:
                        continue
            return {
                "status": "ok",
                "type": "count",
                "results": counts,
                "diagnostics": diagnostics or None,
            }

        # content mode
        results: list[dict] = []
        for line in payload.splitlines():
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            p, ln, snippet = parts[0], parts[1], parts[2]
            try:
                ln_int = int(ln)
            except ValueError:
                continue
            results.append({"path": p, "line": ln_int, "snippet": snippet})
        warning = None
        if not results and _pattern_has_regex_newline(pattern):
            warning = (
                "0 results. Content search is line-oriented and does not run "
                "ripgrep with --multiline, so `\\n` in the regex does not match "
                "line breaks. Use context=N to inspect neighbors, or escape "
                "as `\\\\n` for a literal backslash+n."
            )
        return {
            "status": "ok",
            "type": "content",
            "results": results[offset : offset + limit],
            "diagnostics": diagnostics or None,
            "warning": warning,
        }
