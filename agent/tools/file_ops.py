"""File operations tools - with precise diff-level editing (fully async I/O)."""

import asyncio
import difflib
import os
from pathlib import Path
from typing import Optional

from .base import BaseTool, ToolResult, registry
from ..core.workspace import WORKSPACE_ROOT


# In testing mode, workspace check is relaxed
_TESTING_MODE = os.getenv("CODING_AGENT_TESTING", "0") == "1"
_CODING_AGENT_ROOT = Path(__file__).parent.parent.parent.resolve()


def _resolve_path(path: str) -> Path:
    """Resolve path relative to WORKSPACE_ROOT if not absolute."""
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    return WORKSPACE_ROOT / p


def _is_within_workspace(path: str) -> bool:
    """Check if a path is within the workspace boundary."""
    if _TESTING_MODE:
        return True  # Relax restriction in test mode
    try:
        resolved = _resolve_path(path).resolve()
        return resolved.is_relative_to(WORKSPACE_ROOT)
    except Exception:
        return False


def _validate_write_path(path: str) -> ToolResult:
    """Validate that a path is within workspace for write operations."""
    if not _is_within_workspace(path):
        return ToolResult(
            success=False,
            content="",
            error=f"Write denied: path '{path}' is outside workspace '{WORKSPACE_ROOT}'"
        )
    return None


def _make_diff(original: str, modified: str, path: str) -> str:
    """Generate a unified diff preview showing only changed hunks."""
    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)

    # Ensure lines end with newline for clean diff
    if original_lines and not original_lines[-1].endswith("\n"):
        original_lines[-1] += "\n"
    if modified_lines and not modified_lines[-1].endswith("\n"):
        modified_lines[-1] += "\n"

    diff = list(
        difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )

    if not diff:
        return "(no changes)"

    # Strip the ---/+++ header lines, keep only the hunk markers and context
    return "".join(diff[2:])  # skip '---' and '+++' lines


class ReadFileTool(BaseTool):
    user_facing_name = "Read"

    is_concurrency_safe = True
    is_read_only = True
    name = "read_file"
    description = "Read the contents of a file"

    def render_call(self, args: dict) -> str:
        path = args.get("path", "?")
        limit = args.get("limit", 0)
        if limit:
            return f"Read · {path} (L{args.get('offset',1)}-{args.get('offset',1)+limit-1})"
        return f"Read · {path}"

    def render_result(self, result: "ToolResult") -> str:
        if result.success and result.metadata:
            lines = result.metadata.get("lines", 0)
            return f"Read · {lines} lines"
        return super().render_result(result)

    async def execute(self, path: str, offset: int = 1, limit: int = 0, **kwargs) -> ToolResult:
        try:
            file_path = _resolve_path(path)
            if not await asyncio.to_thread(file_path.exists):
                return ToolResult(success=False, content="", error=f"File not found: {path}")

            content = await asyncio.to_thread(file_path.read_text, "utf-8")
            lines = content.splitlines()
            total = len(lines)

            start = max(0, offset - 1)
            end = len(lines) if limit == 0 else start + limit
            selected = lines[start:end]

            numbered = []
            for i, line in enumerate(selected, start=start + 1):
                numbered.append(f"{i:4d} | {line}")

            header = f"{path} ({total} lines total)"
            if limit > 0:
                header += f" -- showing lines {start + 1}-{min(end, total)}"
            return ToolResult(success=True, content=f"{header}\n" + "\n".join(numbered),
                            metadata={"lines": len(selected), "total_lines": total})
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

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
                        "path": {"type": "string"},
                        "offset": {"type": "integer", "default": 1, "description": "Starting line number (1-based)"},
                        "limit": {"type": "integer", "default": 0, "description": "Max lines to read (0 = all)"},
                    },
                    "required": ["path"],
                },
            },
        }


class WriteFileTool(BaseTool):
    user_facing_name = "Write"

    name = "write_file"
    description = "Write content to a file (creates or overwrites)"

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
                        "path": {"type": "string", "description": "File path to write"},
                        "content": {"type": "string", "description": "File content"},
                    },
                    "required": ["path", "content"],
                },
            },
        }

    async def execute(self, path: str, content: str, **kwargs) -> ToolResult:
        try:
            if error := _validate_write_path(path):
                return error
            file_path = _resolve_path(path)
            await asyncio.to_thread(file_path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(file_path.write_text, content, "utf-8")
            return ToolResult(success=True, content=f"Written to {path}")
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))


class ListFilesTool(BaseTool):
    user_facing_name = "List"

    is_concurrency_safe = True
    is_read_only = True
    name = "list_files"
    description = "List files in a directory"

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
                        "path": {"type": "string", "description": "Directory path", "default": "."},
                    },
                },
            },
        }

    async def execute(self, path: str = ".", **kwargs) -> ToolResult:
        try:
            dir_path = _resolve_path(path)
            if not await asyncio.to_thread(dir_path.exists):
                return ToolResult(success=False, content="", error=f"Directory not found: {path}")

            items = await asyncio.to_thread(lambda: sorted(dir_path.iterdir()))
            result_lines = []
            for item in items:
                item_type = "dir" if item.is_dir() else "file"
                result_lines.append(f"[{item_type}] {item.name}")

            return ToolResult(success=True, content="\n".join(result_lines) if result_lines else "(empty)")
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))


class ApplyDiffTool(BaseTool):
    user_facing_name = "Edit"

    name = "apply_diff"
    description = "Search and replace a block of text in a file (preserves surrounding lines)"

    async def execute(self, path: str, search: str, replace: str, **kwargs) -> ToolResult:
        try:
            if error := _validate_write_path(path):
                return error
            file_path = _resolve_path(path)
            if not await asyncio.to_thread(file_path.exists):
                return ToolResult(success=False, content="", error=f"File not found: {path}")

            original = await asyncio.to_thread(file_path.read_text, "utf-8")
            if search not in original:
                return ToolResult(success=False, content="", error=f"Search text not found in {path}")

            # Normalize line endings for matching
            search_norm = search.replace("\r\n", "\n")
            original_norm = original.replace("\r\n", "\n")
            modified = original_norm.replace(search_norm, replace.replace("\r\n", "\n"), 1)

            if modified == original_norm:
                return ToolResult(success=False, content="", error="No changes made")

            preview = _make_diff(original_norm, modified, path)
            await asyncio.to_thread(file_path.write_text, modified, "utf-8")
            return ToolResult(success=True, content=f"Applied diff to {path}:\n{preview}")
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

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
                        "path": {"type": "string"},
                        "search": {"type": "string", "description": "Exact text block to find"},
                        "replace": {"type": "string", "description": "Replacement text block"},
                    },
                    "required": ["path", "search", "replace"],
                },
            },
        }


class InsertAfterLineTool(BaseTool):
    user_facing_name = "Insert"
    name = "insert_after_line"
    description = "Insert content after a specific line number"

    async def execute(self, path: str, line: int, content: str, **kwargs) -> ToolResult:
        try:
            if error := _validate_write_path(path):
                return error
            file_path = _resolve_path(path)
            if not await asyncio.to_thread(file_path.exists):
                return ToolResult(success=False, content="", error=f"File not found: {path}")

            original_content = await asyncio.to_thread(file_path.read_text, "utf-8")
            original_lines = original_content.splitlines(keepends=True)
            idx = max(0, min(line, len(original_lines)))

            # Ensure the inserted content ends with newline if not already
            insert = content if content.endswith("\n") else content + "\n"
            if not insert.startswith("\n") and idx < len(original_lines) and not original_lines[idx].endswith("\n"):
                insert = "\n" + insert

            modified_lines = original_lines[:idx] + [insert] + original_lines[idx:]
            modified = "".join(modified_lines)
            original = "".join(original_lines)

            preview = _make_diff(original, modified, path)
            await asyncio.to_thread(file_path.write_text, modified, "utf-8")
            return ToolResult(success=True, content=f"Inserted into {path} after line {line}:\n{preview}")
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

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
                        "path": {"type": "string"},
                        "line": {"type": "integer", "description": "Line number after which to insert (1-based)"},
                        "content": {"type": "string", "description": "Content to insert"},
                    },
                    "required": ["path", "line", "content"],
                },
            },
        }


class ReplaceLinesTool(BaseTool):
    user_facing_name = "Replace"
    name = "replace_lines"
    description = "Replace a range of lines with new content"

    async def execute(self, path: str, start: int, end: int, content: str, **kwargs) -> ToolResult:
        try:
            if error := _validate_write_path(path):
                return error
            file_path = _resolve_path(path)
            if not await asyncio.to_thread(file_path.exists):
                return ToolResult(success=False, content="", error=f"File not found: {path}")

            original_content = await asyncio.to_thread(file_path.read_text, "utf-8")
            original_lines = original_content.splitlines(keepends=True)
            s = max(0, start - 1)
            e = min(end, len(original_lines))

            if s >= e:
                return ToolResult(success=False, content="", error="Invalid line range")

            insert = content if content.endswith("\n") else content + "\n"
            modified_lines = original_lines[:s] + [insert] + original_lines[e:]
            modified = "".join(modified_lines)
            original = "".join(original_lines)

            preview = _make_diff(original, modified, path)
            await asyncio.to_thread(file_path.write_text, modified, "utf-8")
            return ToolResult(success=True, content=f"Replaced lines {start}-{end} in {path}:\n{preview}")
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

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
                        "path": {"type": "string"},
                        "start": {"type": "integer", "description": "Start line (1-based, inclusive)"},
                        "end": {"type": "integer", "description": "End line (1-based, inclusive)"},
                        "content": {"type": "string", "description": "Replacement content"},
                    },
                    "required": ["path", "start", "end", "content"],
                },
            },
        }


# Register tools
registry.register(ReadFileTool())
registry.register(WriteFileTool())
registry.register(ListFilesTool())
registry.register(ApplyDiffTool())
registry.register(InsertAfterLineTool())
registry.register(ReplaceLinesTool())
