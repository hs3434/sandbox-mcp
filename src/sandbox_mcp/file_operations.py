"""File operations executed via a backend's one-off shell commands."""

from __future__ import annotations

import base64
import difflib
import shlex
import uuid

_LINE_FMT = "{n}|{line}"


def _unique_heredoc_tag() -> str:
    """Return a heredoc delimiter that cannot appear in arbitrary content."""
    return f"__SANDBOX_EOF_{uuid.uuid4().hex}__"


class FileOperations:
    """Read/write/patch/search via backend.exec_oneoff."""

    def __init__(self, backend):
        self._backend = backend

    # ---- read / write ----

    def read(self, path: str, machine: str, offset: int = 1,
             limit: int = 500) -> dict:
        sed_range = f"{offset},{offset + limit - 1}p"
        cmd = f"sed -n {shlex.quote(sed_range)} {shlex.quote(path)} 2>/dev/null"
        result = self._backend.exec_oneoff(machine, cmd)
        if result.get("exit_code") not in (0, None):
            suggestions = self._backend.suggest_paths(machine, path)
            return {"status": "not_found", "path": path,
                    "suggestions": suggestions}
        output = result.get("output", "") or ""
        if "\x00" in output:
            return {"status": "binary", "path": path,
                    "error": "binary file not readable as text"}
        lines = output.splitlines()
        numbered = [_LINE_FMT.format(n=offset + i, line=ln)
                    for i, ln in enumerate(lines)]
        return {"status": "ok", "path": path,
                "offset": offset, "limit": limit,
                "output": "\n".join(numbered) + ("\n" if numbered else "")}

    def write(self, path: str, content: str, machine: str) -> dict:
        # Always encode via base64 to avoid heredoc EOF collisions and shell
        # interpretation of special characters in arbitrary user content.
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        mkdir = f"mkdir -p $(dirname {shlex.quote(path)})"
        r = self._backend.exec_oneoff(machine, mkdir)
        if r.get("exit_code") not in (0, None):
            return {"status": "error", "path": path, "stage": "mkdir",
                    "error": r.get("stderr") or "mkdir failed"}
        cmd = (f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}")
        r = self._backend.exec_oneoff(machine, cmd)
        if r.get("exit_code") not in (0, None):
            return {"status": "error", "path": path, "stage": "write",
                    "error": r.get("stderr") or "write failed"}
        check = self._syntax_check(path)
        if check is not None:
            r = self._backend.exec_oneoff(machine, check)
            if r.get("exit_code") not in (0, None):
                return {"status": "error", "path": path, "stage": "syntax_check",
                        "error": r.get("stderr") or "syntax check failed"}
        return {"status": "ok", "path": path}

    def _syntax_check(self, path: str) -> str | None:
        if path.endswith(".py"):
            return f"python -m py_compile {shlex.quote(path)}"
        if path.endswith(".sh"):
            return f"bash -n {shlex.quote(path)}"
        if path.endswith(".json"):
            return (f"python -c 'import json; json.load(open({shlex.quote(path)}))'")
        return None

    # ---- patch ----

    def patch(self, mode: str, machine: str, path: str = "",
              old_string: str = "", new_string: str = "",
              replace_all: bool = False, patch: str = "") -> dict:
        if mode == "replace":
            return self._patch_replace(machine, path, old_string,
                                       new_string, replace_all)
        if mode == "patch":
            return self._patch_apply(machine, patch)
        return {"status": "error", "error": f"Unknown patch mode: {mode}"}

    def _patch_replace(self, machine: str, path: str,
                       old_string: str, new_string: str,
                       replace_all: bool) -> dict:
        result = self._backend.exec_oneoff(machine, f"cat {shlex.quote(path)}")
        if result.get("exit_code") not in (0, None):
            return {"status": "not_found", "path": path}
        original = result.get("output", "") or ""
        count = original.count(old_string)
        fuzzy = False
        if count == 0 and "\n" not in old_string:
            # Only fuzzy-match single-line edits to avoid line-by-line
            # comparison failures on multi-line blocks.
            match = difflib.get_close_matches(
                old_string, original.splitlines(), n=1, cutoff=0.6)
            if match:
                old_string = match[0]
                count = original.count(old_string)
                fuzzy = True
        if count == 0:
            return {"status": "error", "error": "old_string not found"}
        if count > 1 and not replace_all:
            return {"status": "error",
                    "error": f"Multiple matches ({count}); set replace_all=true"}
        replaced = original.replace(old_string, new_string)
        diff = "\n".join(difflib.unified_diff(
            original.splitlines(), replaced.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))
        encoded = base64.b64encode(replaced.encode("utf-8")).decode("ascii")
        self._backend.exec_oneoff(
            machine,
            f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}")
        return {"status": "ok", "path": path, "matches": count,
                "fuzzy": fuzzy, "diff": diff}

    def _patch_apply(self, machine: str, patch_text: str) -> dict:
        if not patch_text.strip():
            return {"status": "error", "error": "patch is empty"}
        encoded = base64.b64encode(patch_text.encode("utf-8")).decode("ascii")
        result = self._backend.exec_oneoff(
            machine, f"echo {shlex.quote(encoded)} | base64 -d | patch -p0")
        if result.get("exit_code") not in (0, None):
            return {"status": "error",
                    "error": result.get("stderr") or "patch failed"}
        return {"status": "ok"}

    # ---- search ----

    def search(self, pattern: str, machine: str,
               search_type: str = "content", path: str = ".",
               file_glob: str = "", limit: int = 50,
               offset: int = 0, output_mode: str = "content",
               context: int = 0) -> dict:
        if search_type == "content":
            rg = ["rg", "--line-number", f"--max-count={limit}"]
            if file_glob:
                rg += ["-g", file_glob]
            if output_mode != "content":
                rg += [f"--{output_mode.replace('_', '-')}"]
            if context:
                rg += [f"-C {context}"]
            rg += [shlex.quote(pattern), shlex.quote(path)]
            cmd = " ".join(rg)
        elif search_type == "files":
            # Do not shlex.quote the pattern: it is a glob (e.g. *.py),
            # not a literal filename.
            cmd = f"find {shlex.quote(path)} -name {pattern} -type f"
        else:
            return {"status": "error",
                    "error": f"Unknown search_type: {search_type}"}
        result = self._backend.exec_oneoff(machine, cmd)
        raw = result.get("output", "") or ""
        if search_type == "files":
            results = [r for r in raw.splitlines() if r]
        else:
            results = []
            for line in raw.splitlines():
                if not line:
                    continue
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    p, ln, snippet = parts[0], parts[1], parts[2]
                    try:
                        ln = int(ln)
                    except ValueError:
                        continue
                    results.append({"path": p, "line": ln, "snippet": snippet})
        return {"status": "ok", "type": search_type,
                "results": results[offset:offset + limit]}
