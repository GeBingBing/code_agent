"""Notebook tools — read and edit Jupyter notebooks (.ipynb)."""

import json
from pathlib import Path

from .base import BaseTool, ToolResult, registry


def _resolve_path(path: str) -> Path:
    p = Path(path)
    return p.resolve() if not p.is_absolute() else p


class NotebookReadTool(BaseTool):
    """Read a Jupyter notebook, returning cells with their outputs."""

    user_facing_name = "Notebook"
    is_concurrency_safe = True
    is_read_only = True

    name = "notebook_read"
    description = (
        "Read a Jupyter notebook (.ipynb) and return all cells. "
        "Shows cell type (code/markdown), source, and outputs for code cells."
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
                        "path": {"type": "string", "description": "Path to .ipynb file"},
                    },
                    "required": ["path"],
                },
            },
        }

    def render_call(self, args: dict) -> str:
        return f"Read · {args.get('path', '?')}"

    def render_result(self, result: ToolResult) -> str:
        if result.success and result.metadata:
            cells = result.metadata.get("cells", 0)
            return f"Notebook · {cells} cells"
        return super().render_result(result)

    async def execute(self, path: str, **kwargs) -> ToolResult:
        try:
            fp = _resolve_path(path)
            if not fp.exists():
                return ToolResult(success=False, content="", error=f"File not found: {path}")
            if fp.suffix != ".ipynb":
                return ToolResult(success=False, content="", error="Not a .ipynb file")

            nb = json.loads(fp.read_text("utf-8"))
            cells = nb.get("cells", [])
            lines = []
            cell_count = 0

            for i, cell in enumerate(cells):
                ctype = cell.get("cell_type", "code")
                source = "".join(cell.get("source", []))
                cell_count += 1

                prefix = f"[{i}]" if ctype == "code" else f"[{i} md]"
                lines.append(f"\n{prefix} {ctype}")
                # Show source (truncated if long)
                for src_line in source.split("\n")[:20]:
                    lines.append(f"  {src_line}")
                if len(source.split("\n")) > 20:
                    lines.append(f"  ... ({len(source.splitlines())} total lines)")

                # Show outputs for code cells
                if ctype == "code":
                    outputs = cell.get("outputs", [])
                    for out in outputs[:3]:
                        otype = out.get("output_type", "?")
                        if otype == "stream":
                            text = "".join(out.get("text", []))[:200]
                            if text.strip():
                                lines.append(f"  out: {text.strip()[:100]}")
                        elif otype == "execute_result":
                            data = out.get("data", {})
                            text = data.get("text/plain", [])
                            if isinstance(text, list):
                                text = "".join(text)
                            lines.append(f"  out: {str(text)[:100]}")
                        elif otype == "error":
                            ename = out.get("ename", "Error")
                            lines.append(f"  err: {ename}")

            return ToolResult(
                success=True,
                content="\n".join(lines),
                metadata={"cells": cell_count, "path": path},
            )
        except json.JSONDecodeError as e:
            return ToolResult(success=False, content="", error=f"Invalid notebook JSON: {e}")
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))


class NotebookEditTool(BaseTool):
    """Edit a Jupyter notebook cell — replace source or insert new cell."""

    user_facing_name = "Notebook"
    is_concurrency_safe = False
    is_read_only = False

    name = "notebook_edit"
    description = (
        "Edit a Jupyter notebook cell. Operations: "
        "'replace' (replace source of existing cell), "
        "'insert' (add new cell at position), "
        "'delete' (remove cell at index)."
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
                        "path": {"type": "string", "description": "Path to .ipynb file"},
                        "cell_index": {
                            "type": "integer",
                            "description": "Cell index (0-based). For insert: where to insert. For delete: which to remove.",
                        },
                        "new_source": {
                            "type": "string",
                            "description": "New cell source content (required for replace and insert)",
                        },
                        "cell_type": {
                            "type": "string",
                            "description": "Cell type: 'code' or 'markdown' (for insert; replace preserves existing type)",
                            "enum": ["code", "markdown"],
                        },
                        "operation": {
                            "type": "string",
                            "description": "Edit operation: replace, insert, or delete",
                            "enum": ["replace", "insert", "delete"],
                        },
                    },
                    "required": ["path", "cell_index", "operation"],
                },
            },
        }

    def render_call(self, args: dict) -> str:
        op = args.get("operation", "edit")
        return f"Notebook · {op} · {args.get('path', '?')}"

    async def execute(
        self,
        path: str,
        cell_index: int,
        operation: str,
        new_source: str = "",
        cell_type: str = "code",
        **kwargs,
    ) -> ToolResult:
        try:
            fp = _resolve_path(path)
            if not fp.exists() and operation != "insert":
                return ToolResult(success=False, content="", error=f"File not found: {path}")

            # Load or create
            if fp.exists():
                if fp.suffix != ".ipynb":
                    return ToolResult(success=False, content="", error="Not a .ipynb file")
                nb = json.loads(fp.read_text("utf-8"))
            else:
                nb = {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}

            cells = nb.get("cells", [])

            if operation == "replace":
                if cell_index < 0 or cell_index >= len(cells):
                    return ToolResult(
                        success=False,
                        content="",
                        error=f"Cell index {cell_index} out of range (0-{len(cells)-1})",
                    )
                cells[cell_index]["source"] = new_source.split("\n")
                # Ensure each line ends with \n (Jupyter convention)
                cells[cell_index]["source"] = [line + "\n" for line in new_source.split("\n")]
                # Clear old outputs
                cells[cell_index]["outputs"] = []
                cells[cell_index]["execution_count"] = None

            elif operation == "insert":
                if cell_index < 0:
                    cell_index = 0
                new_cell = {
                    "cell_type": cell_type,
                    "metadata": {},
                    "source": [line + "\n" for line in new_source.split("\n")],
                }
                if cell_type == "code":
                    new_cell["outputs"] = []
                    new_cell["execution_count"] = None
                cells.insert(min(cell_index, len(cells)), new_cell)

            elif operation == "delete":
                if cell_index < 0 or cell_index >= len(cells):
                    return ToolResult(
                        success=False,
                        content="",
                        error=f"Cell index {cell_index} out of range (0-{len(cells)-1})",
                    )
                cells.pop(cell_index)

            else:
                return ToolResult(
                    success=False, content="", error=f"Unknown operation: {operation}"
                )

            nb["cells"] = cells
            fp.write_text(json.dumps(nb, ensure_ascii=False, indent=1), "utf-8")

            return ToolResult(
                success=True,
                content=f"Notebook {operation} cell [{cell_index}] in {path} ({len(cells)} cells total)",
                metadata={"cells": len(cells), "operation": operation},
            )
        except json.JSONDecodeError as e:
            return ToolResult(success=False, content="", error=f"Invalid notebook JSON: {e}")
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))


registry.register(NotebookReadTool())
registry.register(NotebookEditTool())
