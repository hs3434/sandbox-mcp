"""File operations executed via a backend's one-off shell commands."""

from __future__ import annotations

import difflib
import shlex

_LINE_FMT = "{n}|{line}"


class FileOperations:
    """Read/write/patch/search via backend.exec_oneoff."""

    def __init__(self, backend):
        self._backend = backend

    # ---- read / write ----

    def read(self, path: str, target: str, offset: int = 1,
             limit: int = 500) -> dict:
        sed_range = f"{offset},{offset + limit - 1}p"
        cmd = f"sed -n {shlex.quote(sed_range)} {shlex.quote(path)} 2>/dev/null"
        result = self._backend.exec_oneoff(target, cmd)
        if result.get("exit_code") not in (0, None):
            suggestions = self._backend.suggest_paths(target, path)
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

    def write(self, path: str, content: str, target: str) -> dict:
        self._backend.exec_oneoff(
            target, f"mkdir -p $(dirname {shlex.quote(path)})")
        heredoc = "__SANDBOX_EOF__"
        cmd = (f"cat > {shlex.quote(path)} <<'{heredoc}'\n"
               f"{content}\n{heredoc}")
        self._backend.exec_oneoff(target, cmd)
        check = self._syntax_check(path)
        if check is not None:
            self._backend.exec_oneoff(target, check)
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

    def patch(self, mode: str, target: str, path: str = "",
              old_string: str = "", new_string: str = "",
              replace_all: bool = False, patch: str = "") -> dict:
        if mode == "replace":
            return self._patch_replace(target, path, old_string,
                                       new_string, replace_all)
        if mode == "patch":
            return self._patch_apply(target, patch)
        return {"status": "error", "error": f"Unknown patch mode: {mode}"}

    def _patch_replace(self, target: str, path: str,
                       old_string: str, new_string: str,
                       replace_all: bool) -> dict:
        result = self._backend.exec_oneoff(target, f"cat {shlex.quote(path)}")
        if result.get("exit_code") not in (0, None):
            return {"status": "not_found", "path": path}
        original = result.get("output", "") or ""
        count = original.count(old_string)
        fuzzy = False
        if count == 0:
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
        heredoc = "__SANDBOX_EOF__"
        self._backend.exec_oneoff(
            target, f"cat > {shlex.quote(path)} <<'{heredoc}'\n{replaced}\n{heredoc}")
        return {"status": "ok", "path": path, "matches": count,
                "fuzzy": fuzzy, "diff": diff}

    def _patch_apply(self, target: str, patch_text: str) -> dict:
        if not patch_text.strip():
            return {"status": "error", "error": "patch is empty"}
        heredoc = "__SANDBOX_EOF__"
        result = self._backend.exec_oneoff(
            target, f"patch -p0 <<'{heredoc}'\n{patch_text}\n{heredoc}")
        if result.get("exit_code") not in (0, None):
            return {"status": "error", "error": result.get("stderr") or "patch failed"}
        return {"status": "ok"}

    # ---- search ----

    def search(self, pattern: str, target: str,
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
            cmd = f"find {shlex.quote(path)} -name {shlex.quote(pattern)} -type f"
        else:
            return {"status": "error",
                    "error": f"Unknown search_type: {search_type}"}
        result = self._backend.exec_oneoff(target, cmd)
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
