"""Code search tool - hybrid retrieval over indexed codebase."""

import os
from pathlib import Path
from typing import Optional

from .base import BaseTool, ToolResult, registry
from index.code_indexer import CodeIndexer
from index.retriever import CodeRetriever


# Global shared indexer instance (lazy, persistent)
_global_indexer = None
_global_root_dir = None


def _get_indexer(root_dir: str = ".") -> CodeIndexer:
    """Get or create the global persistent indexer."""
    global _global_indexer, _global_root_dir

    root = Path(root_dir).resolve()
    cache_dir = Path(os.getenv("CODING_AGENT_CACHE_DIR", Path.home() / ".coding-agent" / "cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"index_{abs(hash(str(root)))}.json"

    if _global_indexer is None or _global_root_dir != str(root):
        _global_indexer = CodeIndexer(root)
        _global_root_dir = str(root)

        # Try to load from cache
        if cache_file.exists():
            try:
                mtime_src = max(f.stat().st_mtime for f in root.rglob("*.py") if f.is_file())
                mtime_cache = cache_file.stat().st_mtime
                if mtime_cache >= mtime_src:
                    _global_indexer.load(str(cache_file))
                    return _global_indexer
            except Exception:
                pass

        # Full reindex and save
        _global_indexer.index_project()
        try:
            _global_indexer.save(str(cache_file))
        except Exception:
            pass

    return _global_indexer


class CodeSearchTool(BaseTool):
    user_facing_name = "Code"

    is_concurrency_safe = True
    is_read_only = True
    name = "code_search"
    description = "Search the codebase for symbols, files, or content matching a query. Use semantic=true for related symbols and references."

    def __init__(self, root_dir: str = "."):
        self.root_dir = root_dir
        self._retriever = None

    @property
    def indexer(self):
        return _get_indexer(self.root_dir)

    @property
    def retriever(self):
        if self._retriever is None:
            self._retriever = CodeRetriever(self.indexer)
        return self._retriever

    async def execute(self, query: str, top_k: int = 5, semantic: bool = False, **kwargs) -> ToolResult:
        try:
            if semantic:
                results = self.retriever.semantic_search(query, top_k=top_k)
            else:
                results = self.retriever.search(query, top_k=top_k)
            if not results:
                return ToolResult(success=True, content=f"No results for '{query}'.")

            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. [{r.kind}] {r.name}")
                lines.append(f"   {r.path}:{r.line}")
                if r.snippet:
                    for sline in r.snippet.splitlines():
                        lines.append(f"   {sline}")
                lines.append("")
            return ToolResult(success=True, content="\n".join(lines))
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
                        "query": {"type": "string", "description": "Search query (e.g., 'auth', 'User class')"},
                        "top_k": {"type": "integer", "default": 5, "description": "Max results to return"},
                        "semantic": {"type": "boolean", "default": False, "description": "Include related symbols and references"},
                    },
                    "required": ["query"],
                },
            },
        }


class FindReferencesTool(BaseTool):
    user_facing_name = "Refs"

    is_concurrency_safe = True
    is_read_only = True
    name = "find_references"
    description = "Find all references to a symbol (function, class, method) across the codebase"

    def __init__(self, root_dir: str = "."):
        self.root_dir = root_dir

    @property
    def indexer(self):
        return _get_indexer(self.root_dir)

    async def execute(self, symbol: str, **kwargs) -> ToolResult:
        try:
            refs = self.indexer.find_references(symbol)
            if not refs:
                return ToolResult(success=True, content=f"No references found for '{symbol}'.")

            lines = [f"References to '{symbol}':"]
            for ref in refs[:20]:
                lines.append(f"  {ref['path']}:{ref['line']} — {ref['context']}")
            return ToolResult(success=True, content="\n".join(lines))
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
                        "symbol": {"type": "string", "description": "Symbol name to find references for (e.g., 'User', 'get_auth_token')"},
                    },
                    "required": ["symbol"],
                },
            },
        }


class GetCallGraphTool(BaseTool):
    user_facing_name = "CallGraph"

    is_concurrency_safe = True
    is_read_only = True
    name = "get_call_graph"
    description = "Build a call graph showing which functions call which other functions"

    def __init__(self, root_dir: str = "."):
        self.root_dir = root_dir

    @property
    def indexer(self):
        return _get_indexer(self.root_dir)

    async def execute(self, function: str = "", **kwargs) -> ToolResult:
        try:
            graph = self.indexer.build_call_graph()
            if not graph:
                return ToolResult(success=True, content="No call graph data available.")

            if function:
                if function in graph:
                    lines = [f"'{function}' calls:"]
                    for callee in graph[function]:
                        lines.append(f"  → {callee['name']} ({callee['path']}:{callee['line']})")
                    return ToolResult(success=True, content="\n".join(lines))
                else:
                    # Find callers
                    callers = []
                    for caller, callees in graph.items():
                        if any(c["name"] == function for c in callees):
                            callers.append(caller)
                    if callers:
                        lines = [f"'{function}' is called by:"]
                        for c in callers:
                            lines.append(f"  ← {c}")
                        return ToolResult(success=True, content="\n".join(lines))
                    return ToolResult(success=True, content=f"'{function}' not found in call graph.")

            # Summary
            lines = [f"Call graph summary ({len(graph)} functions):"]
            for caller, callees in sorted(graph.items(), key=lambda x: len(x[1]), reverse=True)[:10]:
                lines.append(f"  {caller} → {len(callees)} calls")
            return ToolResult(success=True, content="\n".join(lines))
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
                        "function": {"type": "string", "description": "Function name to focus on (empty for summary)", "default": ""},
                    },
                },
            },
        }


# Register tools
registry.register(CodeSearchTool())
registry.register(FindReferencesTool())
registry.register(GetCallGraphTool())
