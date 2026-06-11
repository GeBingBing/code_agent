"""Glob tool — pattern-based file search."""

from pathlib import Path

from .base import BaseTool, ToolResult, registry


class GlobTool(BaseTool):
    """Search for files matching a glob pattern."""

    user_facing_name = "Glob"
    is_concurrency_safe = True
    is_read_only = True

    name = "glob"
    description = (
        "Find files matching a glob pattern. Supports ** for recursive search. "
        "Examples: '**/*.py', 'src/**/*.tsx', '*.go'"
    )

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
                            "description": "Glob pattern, e.g. '**/*.py', 'src/**/*.ts'",
                        },
                        "path": {
                            "type": "string",
                            "description": "Base directory (defaults to current directory)",
                            "default": ".",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        }

    def render_call(self, args: dict) -> str:
        return f"Glob · {args.get('pattern', '?')}"

    def render_result(self, result: ToolResult) -> str:
        if result.success and result.metadata:
            count = result.metadata.get("count", 0)
            return f"Glob · {count} files"
        return super().render_result(result)

    async def execute(self, pattern: str, path: str = ".", **kwargs) -> ToolResult:
        try:
            base = Path(path).resolve()
            if not base.exists():
                return ToolResult(success=False, content="", error=f"Directory not found: {path}")

            matches = sorted(base.glob(pattern))
            # Filter out common ignore patterns
            ignored = {
                ".git",
                "__pycache__",
                "node_modules",
                ".venv",
                "venv",
                ".tox",
                ".mypy_cache",
                ".pytest_cache",
                "dist",
                "build",
                ".DS_Store",
                "*.pyc",
            }
            filtered = []
            for m in matches:
                parts = set(m.parts)
                if not parts & ignored and not any(
                    p.startswith(".") and p != "." for p in m.parts if p != base.name
                ):
                    filtered.append(m)

            # Limit output
            max_results = 200
            lines = []
            for m in filtered[:max_results]:
                rel = m.relative_to(base) if m.is_relative_to(base) else m
                suffix = "/" if m.is_dir() else ""
                lines.append(str(rel) + suffix)

            if len(filtered) > max_results:
                lines.append(f"... and {len(filtered) - max_results} more")
            if not lines:
                return ToolResult(
                    success=True,
                    content="No files matched",
                    metadata={"count": 0},
                )

            return ToolResult(
                success=True,
                content="\n".join(lines),
                metadata={"count": len(filtered)},
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))


registry.register(GlobTool())
