"""Diagnostics tools (PR-10).

Lets the agent self-introspect runtime metrics and logs without
relying on an external Grafana / Jaeger dashboard. Useful for the
Evaluator Agent (PR-09) and for debugging.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .base import BaseTool, ToolResult, registry
from ..observability import get_metrics
from ..observability.metrics import _NoOpCounter, _NoOpHistogram


class MetricsQueryTool(BaseTool):
    """Query the engine's own in-process metrics.

    Returns counters (e.g. how many times each tool was called) and
    histograms (e.g. mean tool duration). Only works with in-process
    no-op metrics — when OTel is configured to export to a collector,
    consult that collector instead.
    """

    name = "metrics_query"
    description = (
        "Query the agent's own in-process metrics. Returns JSON with counter "
        "totals and histogram statistics. Useful for self-diagnosing "
        "performance issues. Read-only."
    )
    is_read_only = True
    is_concurrency_safe = True
    user_facing_name = "Metrics"

    @property
    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "description": (
                            "Specific metric name to query "
                            "(e.g. agent_tool_call_total, agent_tool_duration_ms). "
                            "Omit for all metrics."
                        ),
                    },
                },
            },
        }

    async def execute(self, metric: Optional[str] = None, **_) -> ToolResult:
        try:
            m = get_metrics()
            snapshot: dict = {}
            for attr in (
                "tool_call_counter",
                "tool_failure_counter",
                "token_usage_counter",
            ):
                inst = getattr(m, attr, None)
                if isinstance(inst, _NoOpCounter):
                    snapshot[inst.name] = {
                        "type": "counter",
                        "total": inst.value(),
                    }
            hist = getattr(m, "tool_duration", None)
            if isinstance(hist, _NoOpHistogram):
                snapshot[hist.name] = {
                    "type": "histogram",
                    "count": hist.count(),
                    "mean_ms": round(hist.mean(), 2),
                }
            if metric:
                filtered = {k: v for k, v in snapshot.items() if metric in k}
                snapshot = filtered or {"note": f"No metric matching '{metric}'"}
            if not snapshot:
                snapshot = {
                    "note": "OTel exporter configured — metrics flow to collector, not local",
                }
            return ToolResult(
                success=True,
                content=json.dumps(snapshot, indent=2, ensure_ascii=False, default=str),
                metadata={"keys": list(snapshot.keys())},
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"metrics_query failed: {e}")

    def render_call(self, args: dict) -> str:
        m = args.get("metric")
        return f"Metrics: {m or 'all'}"

    def render_result(self, result: ToolResult) -> str:
        if not result.success:
            return result.error or ""
        if result.metadata:
            return f"{len(result.metadata.get('keys', []))} metric(s)"
        return ""


class LogsQueryTool(BaseTool):
    """Read recent log entries from the agent's log file.

    Defaults to `~/.coding-agent/agent.log`. Override with `path` for
    arbitrary log files. Returns the last `limit` lines.
    """

    name = "logs_query"
    description = (
        "Read recent agent log entries. Returns the last `limit` lines from "
        "the structured JSON log file. Read-only."
    )
    is_read_only = True
    is_concurrency_safe = True
    user_facing_name = "Logs"

    @property
    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to return (default 50, max 500)",
                        "minimum": 1,
                        "maximum": 500,
                    },
                    "level": {
                        "type": "string",
                        "description": "Filter by level (DEBUG/INFO/WARNING/ERROR)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Override log file path (default ~/.coding-agent/agent.log)",
                    },
                },
            },
        }

    async def execute(
        self,
        limit: int = 50,
        level: Optional[str] = None,
        path: Optional[str] = None,
        **_,
    ) -> ToolResult:
        try:
            limit = max(1, min(500, int(limit)))
        except (TypeError, ValueError):
            limit = 50
        log_path = Path(path) if path else Path.home() / ".coding-agent" / "agent.log"
        if not log_path.exists():
            return ToolResult(
                success=True,
                content=json.dumps([], ensure_ascii=False),
                metadata={"count": 0, "note": "log file not found"},
            )
        try:
            with log_path.open("r", encoding="utf-8") as f:
                # Read all then tail — fine for typical log sizes (<100MB)
                lines = f.readlines()
        except OSError as e:
            return ToolResult(success=False, content="", error=f"read failed: {e}")
        lines = lines[-limit:]
        if level:
            level_upper = level.upper()
            filtered: list = []
            for line in lines:
                try:
                    rec = json.loads(line)
                    if rec.get("level", "").upper() == level_upper:
                        filtered.append(line)
                except json.JSONDecodeError:
                    continue
            lines = filtered
        return ToolResult(
            success=True,
            content="".join(lines),
            metadata={"count": len(lines), "path": str(log_path)},
        )

    def render_call(self, args: dict) -> str:
        limit = args.get("limit", 50)
        level = args.get("level")
        return f"Logs: last={limit}" + (f" level={level}" if level else "")

    def render_result(self, result: ToolResult) -> str:
        if not result.success:
            return result.error or ""
        if result.metadata:
            return f"{result.metadata.get('count', 0)} line(s)"
        return ""


# Register
registry.register(MetricsQueryTool())
registry.register(LogsQueryTool())
