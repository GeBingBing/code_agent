"""Task state machine hook (PR-19, extracted from AgentEngine).

Records each completed step in the task state machine, including a
short hash of the tool result for tamper detection. Persists after
every AFTER_TOOL_EXECUTION fire. Persistence errors are swallowed —
the state machine is best-effort and must never break tool execution.

Originally `AgentEngine._task_state_record_step` — extracted to its
own class so the engine doesn't need to know about persistence.
"""

from __future__ import annotations

import hashlib
from typing import Any


class TaskStateRecordStepHook:
    """Append a (tool, args, result_hash) row to the task state machine.

    Constructor takes the state-machine instance. The hook runs on
    AFTER_TOOL_EXECUTION.
    """

    def __init__(self, task_state_machine):
        self._state = task_state_machine

    async def __call__(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        tool = payload.get("tool", "")
        args = payload.get("args", {})
        result = payload.get("result")
        # Hash the result for tamper detection
        result_hash = ""
        if result is not None:
            try:
                result_hash = hashlib.sha256(
                    str(result).encode("utf-8", errors="replace")
                ).hexdigest()[:16]
            except Exception:
                result_hash = "error"
        # Don't let persistence errors break tool execution
        try:
            self._state.record_completed_step(tool, args, result_hash)
        except Exception:
            pass
        return payload
