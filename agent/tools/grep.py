"""Grep tool - full-text search across project files."""

import re
from pathlib import Path

from .base import BaseTool, ToolResult, registry


class GrepTool(BaseTool):
    user_facing_name = "Search"

    is_concurrency_safe = True
    is_read_only = True
    name = "grep"
    description = "Search for text patterns across all files in the project (full-text search)"

    SKIP_DIRS = {
        "__pycache__",
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
    }

    def __init__(self, root_dir: str = "."):
        self.root_dir = Path(root_dir).resolve()

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str = "*",
        case_sensitive: bool = False,
        max_results: int = 20,
        **kwargs,
    ) -> ToolResult:
        """Search for pattern in files under path.

        Args:
            pattern: Text or regex pattern to search for
            path: Directory to search in (relative to root)
            glob: File glob pattern, e.g. "*.py", "*.js"
            case_sensitive: Whether matching is case-sensitive
            max_results: Max number of matching lines to return
        """
        # Resolve path safely
        raw_path = Path(path)
        if raw_path.is_absolute():
            # Block path traversal sequences in absolute paths
            if ".." in str(raw_path):
                return ToolResult(
                    success=False, content="", error=f"Path traversal blocked: {path}"
                )
            search_path = raw_path.resolve()
        else:
            # Relative path: resolve within root_dir, block traversal
            search_path = (self.root_dir / path).resolve()
            try:
                search_path.relative_to(self.root_dir)
            except ValueError:
                return ToolResult(
                    success=False, content="", error=f"Path escapes workspace: {path}"
                )
        if not search_path.exists():
            return ToolResult(success=False, content="", error=f"Path not found: {path}")

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(success=False, content="", error=f"Invalid regex: {e}")

        matches = []
        file_count = 0

        for filepath in search_path.rglob(glob):
            if self._should_skip(filepath):
                continue
            if not filepath.is_file():
                continue
            if filepath.stat().st_size > 5 * 1024 * 1024:  # skip files > 5MB
                continue

            file_count += 1
            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue

            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel_path = filepath.relative_to(search_path)
                    matches.append(f"{rel_path}:{i}: {line.strip()}")
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break

        if not matches:
            return ToolResult(
                success=True,
                content=f"No matches for '{pattern}' in {path} (searched {file_count} files)",
            )

        header = f"Found {len(matches)} match(es) for '{pattern}' in {path}"
        if len(matches) == max_results:
            header += f" (showing first {max_results})"
        return ToolResult(success=True, content=f"{header}\n" + "\n".join(matches))

    def _should_skip(self, filepath: Path) -> bool:
        return any(part in self.SKIP_DIRS for part in filepath.parts)

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Text or regex pattern to search",
                        },
                        "path": {
                            "type": "string",
                            "default": ".",
                            "description": "Directory to search",
                        },
                        "glob": {
                            "type": "string",
                            "default": "*",
                            "description": "File glob pattern (e.g. '*.py')",
                        },
                        "case_sensitive": {
                            "type": "boolean",
                            "default": False,
                            "description": "Case-sensitive matching",
                        },
                        "max_results": {
                            "type": "integer",
                            "default": 20,
                            "description": "Max results",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        }


# Register tool
registry.register(GrepTool())
