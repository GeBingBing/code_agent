"""Multi-file refactoring tools — safe batch rename with cross-file awareness.

Uses the code indexer to find all references before applying changes.
"""

import ast
import re
from pathlib import Path
from typing import List, Optional, Tuple

from index.code_indexer import CodeIndexer

from ..core.workspace import get_workspace_root
from .base import BaseTool, ToolResult, registry


def _workspace() -> Path:
    """Re-resolve on each call so monkeypatched env vars take effect."""
    return get_workspace_root()


def _validate_python_syntax(path: Path) -> Optional[str]:
    """Validate Python file syntax. Returns error message or None."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        ast.parse(content)
        return None
    except SyntaxError as e:
        return f"Syntax error in {path}: line {e.lineno}, {e.msg}"
    except Exception as e:
        return f"Failed to validate {path}: {e}"


class SafeRenameTool(BaseTool):
    user_facing_name = "Rename"

    """Safely rename a symbol across multiple files.

    Steps:
    1. Index the codebase to find all references
    2. Preview changes (dry_run)
    3. Apply edits with word-boundary matching
    4. Validate Python syntax after changes
    """

    name = "safe_rename"
    description = "Safely rename a symbol (function, class, variable) across all files in the workspace. Use dry_run=True to preview changes first."

    async def execute(
        self,
        symbol: str,
        new_name: str,
        dry_run: bool = False,
        path: str = "",
        **kwargs,
    ) -> ToolResult:
        """Rename a symbol safely across the codebase.

        Args:
            symbol: Current symbol name (e.g., "old_func" or "MyClass.method")
            new_name: New symbol name
            dry_run: If True, only preview changes without applying
            path: Optional subdirectory to limit search scope
        """
        if not symbol or not new_name:
            return ToolResult(
                success=False, content="", error="Both 'symbol' and 'new_name' are required"
            )

        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", new_name):
            return ToolResult(success=False, content="", error=f"Invalid identifier: '{new_name}'")

        workspace = _workspace()
        search_root = workspace / path if path else workspace
        if not search_root.exists():
            return ToolResult(success=False, content="", error=f"Path not found: {search_root}")

        # Index the codebase
        indexer = CodeIndexer(str(search_root))
        indexer.index_project()

        if not indexer.files:
            return ToolResult(success=False, content="", error="No source files found in workspace")

        # Find the symbol definition
        definition_locations: List[Tuple[str, int]] = []  # (path, line)
        for fpath, file_idx in indexer.files.items():
            for sym in file_idx.symbols:
                if sym.name == symbol or sym.name.endswith(f".{symbol}"):
                    definition_locations.append((fpath, sym.line))

        # Find all references (uses) of the symbol
        refs = indexer.find_references(symbol)

        if not definition_locations and not refs:
            return ToolResult(
                success=False,
                content="",
                error=f"Symbol '{symbol}' not found in codebase. Try indexing first with code_search.",
            )

        # Build changes: group by file
        changes_by_file: dict[str, list[dict]] = {}

        # Add definition lines as rename targets
        for fpath, line_no in definition_locations:
            if fpath not in changes_by_file:
                changes_by_file[fpath] = []
            changes_by_file[fpath].append(
                {
                    "line": line_no,
                    "type": "definition",
                }
            )

        # Add references
        for ref in refs:
            fpath = ref["path"]
            if fpath not in changes_by_file:
                changes_by_file[fpath] = []
            changes_by_file[fpath].append(
                {
                    "line": ref["line"],
                    "context": ref["context"],
                    "type": "reference",
                }
            )

        # Sort by line descending so we can edit from bottom to top without offset shifts
        for fpath in changes_by_file:
            changes_by_file[fpath].sort(key=lambda c: c["line"], reverse=True)

        # Preview or apply
        preview_lines = []
        preview_lines.append(f"{'[DRY RUN] ' if dry_run else ''}Renaming '{symbol}' → '{new_name}'")
        preview_lines.append(f"  Definition(s): {len(definition_locations)}")
        preview_lines.append(f"  Reference(s):  {len(refs)}")
        preview_lines.append(f"  File(s):       {len(changes_by_file)}")
        preview_lines.append("")

        applied_files = []
        syntax_errors = []
        base_symbol = symbol.split(".")[-1]

        for fpath, changes in sorted(changes_by_file.items()):
            file_path = search_root / fpath
            if not file_path.exists():
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
            except Exception as e:
                preview_lines.append(f"  ✗ {fpath}: read error ({e})")
                continue

            modified = False
            file_preview = [f"  {fpath}:"]

            for change in changes:
                line_no = change["line"]
                if line_no < 1 or line_no > len(lines):
                    continue

                original_line = lines[line_no - 1]
                # Use word-boundary replacement to avoid partial matches
                new_line = re.sub(rf"\b{re.escape(base_symbol)}\b", new_name, original_line)
                if new_line != original_line:
                    lines[line_no - 1] = new_line
                    modified = True
                    marker = "[def]" if change.get("type") == "definition" else "[ref]"
                    file_preview.append(
                        f"    L{line_no:4d} {marker}  - {original_line.strip()[:60]}"
                    )
                    file_preview.append(f"    L{line_no:4d} {marker}  + {new_line.strip()[:60]}")

            if not modified:
                continue

            preview_lines.extend(file_preview)

            if not dry_run:
                try:
                    file_path.write_text(
                        "\n".join(lines) + ("\n" if content.endswith("\n") else ""),
                        encoding="utf-8",
                    )
                    applied_files.append(fpath)

                    # Validate Python syntax
                    if fpath.endswith(".py"):
                        err = _validate_python_syntax(file_path)
                        if err:
                            syntax_errors.append(err)
                except Exception as e:
                    syntax_errors.append(f"Failed to write {fpath}: {e}")

        preview_lines.append("")
        if dry_run:
            preview_lines.append(f"Use dry_run=False to apply {len(changes_by_file)} file changes.")
        else:
            preview_lines.append(f"Applied changes to {len(applied_files)} file(s).")
            if syntax_errors:
                preview_lines.append("")
                preview_lines.append("⚠ Syntax validation issues:")
                for err in syntax_errors:
                    preview_lines.append(f"  {err}")

        return ToolResult(
            success=len(syntax_errors) == 0,
            content="\n".join(preview_lines),
            error="\n".join(syntax_errors) if syntax_errors else None,
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
                        "symbol": {
                            "type": "string",
                            "description": "Current symbol name to rename (e.g., 'old_function' or 'MyClass.method')",
                        },
                        "new_name": {
                            "type": "string",
                            "description": "New name for the symbol",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, only preview changes without applying",
                        },
                        "path": {
                            "type": "string",
                            "default": "",
                            "description": "Optional subdirectory to limit search scope",
                        },
                    },
                    "required": ["symbol", "new_name"],
                },
            },
        }


class GetRefactorPreviewTool(BaseTool):
    user_facing_name = "Preview"

    is_concurrency_safe = True
    is_read_only = True
    """Show a preview of what safe_rename would change without applying."""

    name = "get_refactor_preview"
    description = (
        "Preview all changes that would be made by safe_rename without modifying any files"
    )

    async def execute(self, symbol: str, new_name: str, path: str = "", **kwargs) -> ToolResult:
        """Delegate to SafeRenameTool with dry_run=True."""
        tool = SafeRenameTool()
        return await tool.execute(symbol=symbol, new_name=new_name, dry_run=True, path=path)

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
                        "symbol": {"type": "string", "description": "Symbol to rename"},
                        "new_name": {"type": "string", "description": "Proposed new name"},
                        "path": {
                            "type": "string",
                            "default": "",
                            "description": "Subdirectory scope",
                        },
                    },
                    "required": ["symbol", "new_name"],
                },
            },
        }


# Register tools
registry.register(SafeRenameTool())
registry.register(GetRefactorPreviewTool())
