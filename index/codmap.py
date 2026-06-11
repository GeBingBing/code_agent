"""Codmap — Aider-style "repo map" injected before LLM calls (PR-05).

Generates a compact, readable map of the workspace's code files: each file
gets a one-line header (path, line count, age) followed by up to N top-level
symbol signatures. Designed to be **injected as a system-reminder** (not into
the system prompt itself) so the LLM sees project structure on every turn
without busting the prompt cache.

Why a code map?
- An LLM dropped into a fresh project has no idea what files exist.
- Tools like `list_files` cost tool-call steps; the map is free context.
- Maps have been shown to materially improve code-edit accuracy in aider's
  benchmarks — and many LLMs are trained on aider-style output.

Why **system-reminder** rather than the system prompt?
- The system prompt is often cached by inference providers (cache hit ⇒ fast
  + cheap). Mutating it per-call would defeat the cache.
- system-reminder is the same channel we use for `cwd` / `git status` —
  it lives one layer deeper and is intentionally re-injected.
"""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java"}
SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", "venv", ".venv",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "workspace",  # not the user's source tree
}


@dataclass
class FileEntry:
    """One file's contribution to the codmap."""
    path: str
    line_count: int
    mtime: float
    symbols: List[str] = field(default_factory=list)  # top-level signatures

    def header_line(self, now: Optional[float] = None) -> str:
        """`path/to/file.py (340 lines, mod 2d ago)`."""
        age = self._age_str(now=now)
        return f"{self.path} ({self.line_count} lines, {age})"

    def _age_str(self, now: Optional[float] = None) -> str:
        now = now if now is not None else time.time()
        delta = max(0.0, now - self.mtime)
        if delta < 60:
            return "just now"
        if delta < 3600:
            m = int(delta // 60)
            return f"mod {m}m ago"
        if delta < 86400:
            h = int(delta // 3600)
            return f"mod {h}h ago"
        d = int(delta // 86400)
        if d < 30:
            return f"mod {d}d ago"
        mo = int(d // 30)
        if mo < 12:
            return f"mod {mo}mo ago"
        y = int(d // 365)
        return f"mod {y}y ago"

    def render(self, max_symbols: int = 5, now: Optional[float] = None) -> List[str]:
        """Render this entry as one or more text lines."""
        lines = [self.header_line(now=now)]
        for sym in self.symbols[:max_symbols]:
            lines.append(f"  {sym}")
        return lines


# ── Symbol extractors (lightweight, no index dependency) ─────────


def extract_python_symbols(path: Path) -> List[str]:
    """Top-level class/def signatures from a Python file.

    Returns up to MAX_SYMBOLS strings like:
        class AuthService:
        def verify_token(token: str) -> bool
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    sigs: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            sigs.append(f"class {node.name}:")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            sigs.append(f"{prefix} {_format_args(node)}{_format_returns(node)}")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    sigs.append(f"{t.id} = …")
    return sigs


def _format_args(fn) -> str:
    """Best-effort function argument rendering from AST."""
    args: List[str] = []
    pos = getattr(fn.args, "posonlyargs", []) + getattr(fn.args, "args", [])
    for a in pos:
        args.append(a.arg)
    if fn.args.vararg:
        args.append(f"*{fn.args.vararg.arg}")
    for a in fn.args.kwonlyargs:
        args.append(f"{a.arg}=…")
    if fn.args.kwarg:
        args.append(f"**{fn.args.kwarg.arg}")
    return f"{fn.name}({', '.join(args)})"


def _format_returns(fn) -> str:
    """Render return annotation if present and simple."""
    if fn.returns is None:
        return ""
    try:
        return f" -> {ast.unparse(fn.returns)}"
    except Exception:
        return ""


_JS_TS_PATTERNS = [
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)"),
    re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*[=:]\s*(?:function|\()"),
]


def extract_js_symbols(path: Path) -> List[str]:
    """Top-level function/class signatures from JS/TS file."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    sigs: List[str] = []
    for line in lines[:200]:  # cap scan
        for pat in _JS_TS_PATTERNS:
            m = pat.match(line)
            if m:
                if m.lastindex and m.lastindex >= 2:
                    args = m.group(2).strip()
                    sigs.append(f"function {m.group(1)}({args})")
                else:
                    sigs.append(f"class {m.group(1)}")
                break
    return sigs


# ── Generator ─────────────────────────────────────────────────────


class CodmapGenerator:
    """Generates a compact text map of a workspace's source files.

    Caches per-file mtimes so re-generation only re-parses changed files
    (mtime-based incremental scan).
    """

    DEFAULT_MAX_FILES = 50
    DEFAULT_MAX_TOTAL_KB = 5
    DEFAULT_MAX_SYMBOLS_PER_FILE = 5

    def __init__(self, workspace: Path, max_files: int = DEFAULT_MAX_FILES,
                 max_total_kb: int = DEFAULT_MAX_TOTAL_KB,
                 max_symbols_per_file: int = DEFAULT_MAX_SYMBOLS_PER_FILE):
        self.workspace = Path(workspace).resolve()
        self.max_files = max_files
        self.max_total_kb = max_total_kb
        self.max_symbols_per_file = max_symbols_per_file
        self._cache: Dict[str, FileEntry] = {}
        self._cache_mtime: Dict[str, float] = {}

    def generate(self, max_files: Optional[int] = None,
                 max_total_kb: Optional[int] = None) -> str:
        """Build the codmap text. Returns empty string for empty workspaces."""
        max_files = max_files if max_files is not None else self.max_files
        max_total_kb = max_total_kb if max_total_kb is not None else self.max_total_kb

        entries = self._scan()
        if not entries:
            return ""

        # Sort: most recently modified first, larger files first as tiebreaker
        entries.sort(key=lambda e: (-e.mtime, -e.line_count))

        # Truncate to max_files
        entries = entries[:max_files]

        # Emit lines until byte budget exhausted
        now = time.time()
        out_lines: List[str] = []
        total_bytes = 0
        budget = max_total_kb * 1024
        for entry in entries:
            entry_lines = entry.render(max_symbols=self.max_symbols_per_file, now=now)
            entry_text = "\n".join(entry_lines)
            entry_bytes = len(entry_text.encode("utf-8"))
            # Always include the first file (header at minimum)
            if total_bytes > 0 and total_bytes + entry_bytes > budget:
                break
            out_lines.extend(entry_lines)
            total_bytes += entry_bytes
        return "\n".join(out_lines)

    # ── Internals ────────────────────────────────────────────────

    def _scan(self) -> List[FileEntry]:
        result: List[FileEntry] = []
        for path in self._iter_source_files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            rel = str(path.relative_to(self.workspace))
            cached = self._cache.get(rel)
            if cached is not None and self._cache_mtime.get(rel) == mtime:
                result.append(cached)
                continue
            entry = FileEntry(
                path=rel,
                line_count=_count_lines(path),
                mtime=mtime,
                symbols=self._extract_symbols(path),
            )
            self._cache[rel] = entry
            self._cache_mtime[rel] = mtime
            result.append(entry)
        return result

    def _iter_source_files(self):
        """Yield code files in workspace, skipping junk directories."""
        for path in self.workspace.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix not in CODE_EXTS:
                continue
            yield path

    def _extract_symbols(self, path: Path) -> List[str]:
        if path.suffix == ".py":
            return extract_python_symbols(path)
        if path.suffix in {".js", ".ts", ".tsx"}:
            return extract_js_symbols(path)
        return []

    # ── Introspection (for tests) ────────────────────────────────

    def cache_size(self) -> int:
        return len(self._cache)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_mtime.clear()


def _count_lines(path: Path) -> int:
    """Count lines in a text file. Fast path for small files."""
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except (OSError, UnicodeDecodeError):
        return 0
