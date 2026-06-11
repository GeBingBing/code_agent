"""LSP Tool — semantic code understanding via Language Server Protocol.

Operations: goToDefinition, findReferences, hover, documentSymbol, workspaceSymbol.
Delegates to real language servers (gopls, pyright, typescript-language-server, etc.)
for compiler-grade accuracy.
"""

from ..tools.base import BaseTool, ToolResult, registry


class LSPTool(BaseTool):
    user_facing_name = "LSP"

    """Semantic code understanding tool backed by Language Server Protocol.

    Requires an LSP server to be installed for the target language:
    - Python: pyright (pip install pyright) or pylsp
    - Go: gopls (go install golang.org/x/tools/gopls@latest)
    - TypeScript/JavaScript: typescript-language-server (npm install -g)
    - Rust: rust-analyzer
    - Java: jdtls
    """

    name = "lsp"
    description = (
        "Semantic code understanding via LSP. Operations: "
        "goToDefinition (jump to symbol definition), "
        "findReferences (all references to a symbol, semantically accurate), "
        "hover (type info and documentation), "
        "documentSymbol (all symbols in a file), "
        "workspaceSymbol (search symbols across project)."
    )
    is_read_only = True
    is_concurrency_safe = False  # LSP servers are stateful

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
                        "operation": {
                            "type": "string",
                            "description": (
                                "LSP operation: goToDefinition, findReferences, "
                                "hover, documentSymbol, workspaceSymbol, "
                                "incomingCalls, outgoingCalls"
                            ),
                            "enum": [
                                "goToDefinition", "findReferences", "hover",
                                "documentSymbol", "workspaceSymbol",
                                "incomingCalls", "outgoingCalls",
                            ],
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Absolute or relative file path",
                        },
                        "line": {
                            "type": "integer",
                            "description": "Line number (1-based). Required for goToDefinition, findReferences, hover, incomingCalls, outgoingCalls",
                        },
                        "character": {
                            "type": "integer",
                            "description": "Character offset on line (1-based)",
                        },
                        "query": {
                            "type": "string",
                            "description": "Symbol name to search. Required for workspaceSymbol",
                        },
                    },
                    "required": ["operation", "file_path"],
                },
            },
        }

    def render_call(self, args: dict) -> str:
        op = args.get("operation", "?")
        fp = args.get("file_path", "?")
        return f"LSP · {op} · {fp}"

    def render_result(self, result: "ToolResult") -> str:
        if result.success and result.metadata:
            count = result.metadata.get("count", 0)
            op = result.metadata.get("operation", "")
            return f"LSP · {op} · {count} results"
        return super().render_result(result)

    async def execute(
        self,
        operation: str,
        file_path: str,
        line: int = 1,
        character: int = 1,
        query: str = "",
        **kwargs,
    ) -> ToolResult:
        """Execute an LSP operation."""
        from agent.lsp.client import get_lsp_client, LSPError

        # Normalize path
        from pathlib import Path
        fp = str(Path(file_path).resolve() if not Path(file_path).is_absolute()
                 else Path(file_path))

        client = await get_lsp_client(fp)
        if not client:
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"No LSP server available for {Path(fp).suffix}. "
                    "Install one: pyright (pip install pyright), "
                    "gopls (go install golang.org/x/tools/gopls@latest), "
                    "or typescript-language-server (npm install -g)"
                ),
            )

        # Ensure file is open on the server
        await client.did_open(fp)

        try:
            if operation == "goToDefinition":
                result = await self._go_to_definition(client, fp, line, character)
            elif operation == "findReferences":
                result = await self._find_references(client, fp, line, character)
            elif operation == "hover":
                result = await self._hover(client, fp, line, character)
            elif operation == "documentSymbol":
                result = await self._document_symbol(client, fp)
            elif operation == "workspaceSymbol":
                result = await self._workspace_symbol(client, query)
            elif operation == "incomingCalls":
                result = await self._call_hierarchy(client, fp, line, character, "incoming")
            elif operation == "outgoingCalls":
                result = await self._call_hierarchy(client, fp, line, character, "outgoing")
            else:
                return ToolResult(
                    success=False, content="",
                    error=f"Unknown operation: {operation}"
                )
            return result
        except LSPError as e:
            return ToolResult(success=False, content="", error=str(e))
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

    # ── Operation implementations ──────────────────────────────────

    async def _go_to_definition(self, client, fp, line, character) -> ToolResult:
        params = {
            "textDocument": {"uri": _to_uri(fp)},
            "position": {"line": line - 1, "character": character - 1},
        }
        result = await client.request("textDocument/definition", params)
        if not result:
            return ToolResult(success=False, content="", error="No definition found")

        locations = _as_list(result)
        lines = []
        for loc in locations[:10]:
            path = _from_uri(loc["uri"]) if "uri" in loc else fp
            l = loc["range"]["start"]["line"] + 1
            c = loc["range"]["start"]["character"] + 1
            lines.append(f"{path}:{l}:{c}")
        return ToolResult(
            success=True,
            content="\n".join(lines),
            metadata={"count": len(locations), "operation": "goToDefinition"},
        )

    async def _find_references(self, client, fp, line, character) -> ToolResult:
        params = {
            "textDocument": {"uri": _to_uri(fp)},
            "position": {"line": line - 1, "character": character - 1},
            "context": {"includeDeclaration": True},
        }
        result = await client.request("textDocument/references", params)
        if not result:
            return ToolResult(success=False, content="", error="No references found")

        locations = result if isinstance(result, list) else []
        lines = []
        for loc in locations[:20]:
            path = _from_uri(loc["uri"])
            l = loc["range"]["start"]["line"] + 1
            c = loc["range"]["start"]["character"] + 1
            lines.append(f"{path}:{l}:{c}")
        return ToolResult(
            success=True,
            content="\n".join(lines) if lines else "0 references",
            metadata={"count": len(locations), "operation": "findReferences"},
        )

    async def _hover(self, client, fp, line, character) -> ToolResult:
        params = {
            "textDocument": {"uri": _to_uri(fp)},
            "position": {"line": line - 1, "character": character - 1},
        }
        result = await client.request("textDocument/hover", params)
        if not result:
            return ToolResult(success=False, content="", error="No hover info")

        contents = result.get("contents", {})
        if isinstance(contents, dict):
            text = contents.get("value", str(contents))
        elif isinstance(contents, list):
            text = "\n".join(
                c.get("value", str(c)) if isinstance(c, dict) else str(c)
                for c in contents
            )
        else:
            text = str(contents)

        # Strip markdown if present
        if text.startswith("```") and text.endswith("```"):
            text = text[3:-3].strip()

        return ToolResult(
            success=True,
            content=text[:500],
            metadata={"operation": "hover"},
        )

    async def _document_symbol(self, client, fp) -> ToolResult:
        params = {"textDocument": {"uri": _to_uri(fp)}}
        result = await client.request("textDocument/documentSymbol", params)
        if not result:
            return ToolResult(success=False, content="", error="No symbols found")

        symbols = _flatten_symbols(result)
        lines = []
        for s in symbols[:30]:
            kind = s.get("kind", "?")
            name = s.get("name", "?")
            l = s.get("range", {}).get("start", {}).get("line", 0) + 1
            indent = "  " * (s.get("_depth", 0))
            lines.append(f"{indent}{_kind_icon(kind)} {name} :{l}")
        return ToolResult(
            success=True,
            content="\n".join(lines),
            metadata={"count": len(symbols), "operation": "documentSymbol"},
        )

    async def _workspace_symbol(self, client, query) -> ToolResult:
        if not query:
            return ToolResult(
                success=False, content="", error="query required for workspaceSymbol")
        params = {"query": query}
        result = await client.request("workspace/symbol", params)
        if not result:
            return ToolResult(success=False, content="", error="No symbols found")

        symbols = result if isinstance(result, list) else []
        lines = []
        for s in symbols[:20]:
            name = s.get("name", "?")
            kind = s.get("kind", "?")
            path = _from_uri(s.get("location", {}).get("uri", ""))
            l = s.get("location", {}).get("range", {}).get("start", {}).get("line", 0) + 1
            lines.append(f"{_kind_icon(kind)} {name} · {path}:{l}")
        return ToolResult(
            success=True,
            content="\n".join(lines) if lines else "0 symbols found",
            metadata={"count": len(symbols), "operation": "workspaceSymbol"},
        )

    async def _call_hierarchy(self, client, fp, line, character, direction) -> ToolResult:
        # Step 1: prepare call hierarchy
        params = {
            "textDocument": {"uri": _to_uri(fp)},
            "position": {"line": line - 1, "character": character - 1},
        }
        items = await client.request("textDocument/prepareCallHierarchy", params)
        if not items or not isinstance(items, list) or len(items) == 0:
            return ToolResult(success=False, content="", error="No call hierarchy available")

        item = items[0]
        method = "callHierarchy/incomingCalls" if direction == "incoming" else "callHierarchy/outgoingCalls"
        calls = await client.request(method, {"item": item})
        if not calls:
            return ToolResult(success=False, content="", error="No calls found")

        call_list = calls if isinstance(calls, list) else []
        lines = []
        for call in call_list[:20]:
            name = call.get("from", call).get("name", "?")
            uri = call.get("from", call).get("uri", "")
            path = _from_uri(uri)
            l = call.get("from", call).get("range", {}).get("start", {}).get("line", 0) + 1
            direction_label = "←" if direction == "incoming" else "→"
            lines.append(f"{direction_label} {name} · {path}:{l}")
        return ToolResult(
            success=True,
            content="\n".join(lines) if lines else "0 calls",
            metadata={"count": len(call_list), "operation": f"{direction}Calls"},
        )


# ── Helpers ────────────────────────────────────────────────────────

def _to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _from_uri(uri: str) -> str:
    from urllib.parse import urlparse, unquote
    parsed = urlparse(uri)
    return unquote(parsed.path)


def _as_list(result) -> list:
    """Normalize LSP location result to a list."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "uri" in result:
        return [result]
    return []


def _flatten_symbols(symbols, depth: int = 0) -> list:
    """Flatten hierarchical documentSymbol results."""
    flat = []
    for s in (symbols if isinstance(symbols, list) else [symbols]):
        if isinstance(s, dict):
            s["_depth"] = depth
            flat.append(s)
            children = s.get("children", [])
            if children:
                flat.extend(_flatten_symbols(children, depth + 1))
    return flat


def _kind_icon(kind) -> str:
    """Map LSP SymbolKind (1-27) to icon."""
    if isinstance(kind, int):
        return {
            5: "⬛",   # Class
            6: "⬛",   # Method
            9: "⬛",   # Constructor
            12: "▸",  # Function
            11: "⬛",  # Interface
            13: "▾",  # Variable
            14: "▾",  # Constant
            7: "◈",   # Property
            2: "▣",   # Module
            3: "▣",   # Namespace
        }.get(kind, "○")
    return "○"


# Register
registry.register(LSPTool())
