"""Audit log query tool (PR-08).

Exposes the append-only audit log to the LLM so it can introspect
"what did I just do?" without re-reading the entire conversation.

The tool is read-only and concurrency-safe — it never mutates the log.
"""

from __future__ import annotations

import json
from typing import Optional

from ..core.audit_log import get_audit_logger
from .base import BaseTool, ToolResult, registry


class AuditQueryTool(BaseTool):
    """Query the audit log with optional filters.

    Returns matching records as JSON. Use this to introspect tool
    invocations, permission decisions, and durations across a session.
    """

    name = "audit_query"
    description = (
        "Query the append-only audit log. Filter by agent_id, action "
        "(tool_call|tool_result|permission_check|state_transition), tool name, "
        "or time range (ISO 8601 strings). Returns up to `limit` matching "
        "records as a JSON array. Read-only — never mutates the log."
    )
    is_read_only = True
    is_concurrency_safe = True
    user_facing_name = "AuditQuery"

    @property
    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "ISO 8601 start timestamp (inclusive), e.g. 2026-06-01T00:00:00Z",
                    },
                    "end": {
                        "type": "string",
                        "description": "ISO 8601 end timestamp (inclusive)",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Filter by agent_id (main, orchestrator, sub-xxx)",
                    },
                    "action": {
                        "type": "string",
                        "description": "tool_call | tool_result | permission_check | state_transition",
                    },
                    "tool": {
                        "type": "string",
                        "description": "Filter by tool name (e.g. read_file)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max records to return (default 100, max 1000)",
                        "minimum": 1,
                        "maximum": 1000,
                    },
                },
            },
        }

    async def execute(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        agent_id: Optional[str] = None,
        action: Optional[str] = None,
        tool: Optional[str] = None,
        limit: int = 100,
        **kwargs,
    ) -> ToolResult:
        try:
            limit = max(1, min(1000, int(limit)))
        except (TypeError, ValueError):
            limit = 100
        try:
            audit = get_audit_logger()
            records = audit.query(
                start=start,
                end=end,
                agent_id=agent_id,
                action=action,
                tool=tool,
                limit=limit,
            )
            content = json.dumps(records, indent=2, ensure_ascii=False, default=str)
            return ToolResult(
                success=True,
                content=content,
                metadata={"count": len(records)},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                content="",
                error=f"audit_query failed: {e}",
            )

    def render_call(self, args: dict) -> str:
        parts = []
        for k in ("agent_id", "action", "tool"):
            v = args.get(k)
            if v:
                parts.append(f"{k}={v}")
        return "AuditQuery: " + (", ".join(parts) if parts else "all")

    def render_result(self, result: ToolResult) -> str:
        if not result.success:
            return result.error or "query failed"
        if result.metadata:
            return f"{result.metadata.get('count', 0)} record(s)"
        return ""


# Register
registry.register(AuditQueryTool())
