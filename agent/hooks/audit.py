"""Audit log hook (PR-19, extracted from AgentEngine).

Two hooks:
  - `before_tool`: stamp the payload with `_audit_start_ts` and log
    a `tool_call` record (with the full args dict).
  - `after_tool`: read `_audit_start_ts`, compute duration, log a
    `tool_result` record (with result, error, duration_ms).

Both hooks swallow audit exceptions — audit must never break tool
execution. Originally `AgentEngine._audit_before_tool` and
`_audit_after_tool`.
"""

from __future__ import annotations

import time
from typing import Any, Optional


class AuditHook:
    """Record tool calls + results to the audit log.

    Constructor takes the audit logger and the session/trace id.
    The two call methods (before_tool, after_tool) are designed to be
    registered against BEFORE_TOOL_EXECUTION and AFTER_TOOL_EXECUTION
    respectively.
    """

    def __init__(self, audit, trace_id: str):
        self._audit = audit
        self._trace_id = trace_id

    async def before_tool(self, payload: Any) -> Any:
        """Stamp payload with start time, log the tool_call event."""
        if self._audit is None or not isinstance(payload, dict):
            return payload
        payload["_audit_start_ts"] = time.time()
        tool_name = payload.get("tool", "")
        args = payload.get("args", {})
        try:
            self._audit.log({
                "session_id": self._trace_id,
                "agent_id": "main",
                "action": "tool_call",
                "tool": tool_name,
                "args": args,
            })
        except Exception:
            pass  # Audit must never break tool execution
        return payload

    async def after_tool(self, payload: Any) -> Any:
        """Log the tool_result event with duration + error."""
        if self._audit is None or not isinstance(payload, dict):
            return payload
        start_ts = payload.get("_audit_start_ts")
        duration_ms: Optional[float] = None
        if isinstance(start_ts, (int, float)):
            duration_ms = (time.time() - start_ts) * 1000.0
        tool_name = payload.get("tool", "")
        result = payload.get("result")
        error = payload.get("error")
        try:
            self._audit.log({
                "session_id": self._trace_id,
                "agent_id": "main",
                "action": "tool_result",
                "tool": tool_name,
                "result": result if result is not None else None,
                "duration_ms": duration_ms,
                "error": str(error) if error else None,
            })
        except Exception:
            pass
        return payload
