"""Memory tools — semantic_search (PR-04).

Exposes the L3 long-term vector memory to the LLM as a callable tool.
The agent can call `semantic_search(query, k)` to retrieve memories
semantically related to a natural-language query — even when the query
shares no words with the stored content.
"""

import json
from typing import Any, Dict

from .base import BaseTool, ToolResult, registry


class SemanticSearchTool(BaseTool):
    """LLM-callable wrapper around the L3 long-term vector memory."""

    name = "semantic_search"
    description = (
        "Search the agent's long-term memory semantically. "
        "Returns up to k memories ranked by cosine similarity to the query. "
        "Use this to recall past decisions, user preferences, project conventions, "
        "or lessons learned from earlier sessions."
    )
    user_facing_name = "Memory"
    is_read_only = True
    is_concurrency_safe = True

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
                        "query": {
                            "type": "string",
                            "description": (
                                "Natural-language search query, e.g. "
                                "'how to handle concurrent writes' or 'project lint config'."
                            ),
                        },
                        "k": {
                            "type": "integer",
                            "description": "Number of results to return (default 5, max 50).",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "include_metadata": {
                            "type": "boolean",
                            "description": "If true, include per-hit metadata in the result.",
                            "default": False,
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    async def execute(
        self,
        query: str,
        k: int = 5,
        include_metadata: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        """Search long-term memory semantically.

        Args:
            query: Natural-language search query.
            k: Number of results to return (1..50, default 5).
            include_metadata: Include per-hit metadata in the result.

        Returns:
            ToolResult with JSON payload `{query, count, hits: [...]}`.
        """
        if not query or not query.strip():
            return ToolResult(
                success=False,
                content="",
                error="Query must be a non-empty string",
            )
        if not isinstance(k, int) or k < 1 or k > 50:
            return ToolResult(
                success=False,
                content="",
                error=f"k must be an integer between 1 and 50 (got {k!r})",
            )

        from ..core.vector_memory import get_vector_memory

        vm = get_vector_memory()
        if vm.count() == 0:
            return ToolResult(
                success=True,
                content=json.dumps(
                    {"hits": [], "count": 0, "message": "Long-term memory is empty."},
                    indent=2,
                ),
                metadata={"count": 0, "query": query, "k": k},
            )

        try:
            hits = vm.search(query, top_k=k, return_hits=True)
        except Exception as e:
            return ToolResult(
                success=False,
                content="",
                error=f"semantic_search failed: {e}",
            )

        payload = {
            "query": query,
            "count": len(hits),
            "hits": [h.to_dict(include_metadata=include_metadata) for h in hits],
        }
        return ToolResult(
            success=True,
            content=json.dumps(payload, indent=2, ensure_ascii=False),
            metadata={"count": len(hits), "query": query, "k": k},
        )

    def render_call(self, args: Dict[str, Any]) -> str:
        q = str(args.get("query", ""))[:60]
        k = args.get("k", 5)
        return f"semantic_search: {q} (k={k})"

    def render_result(self, result: ToolResult) -> str:
        if not result.success:
            return (result.error or "search failed")[:80]
        meta = result.metadata or {}
        return f"{meta.get('count', 0)} hits"


# ── Module-level singleton + registration ──────────────────────────


semantic_search_tool = SemanticSearchTool()
registry.register(semantic_search_tool)
