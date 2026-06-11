"""Progress anchor hooks (PR-19, extracted from AgentEngine).

Two hooks:
  - `inject`: BEFORE_LLM_CALL — read .claude-progress.txt and
    prepend a `<system-reminder>` block to the last user message.
    Provides cross-session resume.
  - `update`: AFTER_TOOL_EXECUTION — increment the step counter,
    update known_issues, recompute the chain hash, write atomically.

Both hooks swallow exceptions. Originally `AgentEngine._inject_progress_hook`
and `_update_progress_hook`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ..core.progress_anchor import ProgressRecord


def extract_step_num(step_str: str) -> int:
    """Extract the leading integer from a step string like '3/8 (last: x)'."""
    if not step_str:
        return 0
    m = re.match(r"(\d+)/", step_str)
    return int(m.group(1)) if m else 0


class ProgressInjectHook:
    """Inject the progress anchor as a system-reminder on each LLM call.

    Idempotent: skips if the last user message already contains a
    `<progress>` block (so a prior hook wins, no double-injection).
    """

    def __init__(self, anchor):
        self._anchor = anchor

    async def __call__(self, payload: Any) -> Any:
        if self._anchor is None or not isinstance(payload, dict):
            return payload
        try:
            record = self._anchor.read()
            if record is None or record.is_empty():
                return payload
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                return payload
            reminder = (
                "<system-reminder>\n"
                "<progress>\n"
                f"{record.to_prompt()}\n"
                "</progress>\n"
                "</system-reminder>"
            )
            for msg in reversed(messages):
                if getattr(msg, "role", None) == "user":
                    existing = msg.content or ""
                    if "<progress>" in existing:
                        break
                    msg.content = f"{existing}\n{reminder}" if existing else reminder
                    break
        except Exception:
            pass
        return payload


class ProgressUpdateHook:
    """Update the progress anchor after a tool call.

    Constructor takes the anchor, max_steps (for the "N/M" counter),
    and a small bundle of accessors for the engine's running state
    (current plan + last task name). This keeps the hook loosely
    coupled to AgentEngine — the engine passes lambdas, not itself.
    """

    def __init__(self, anchor, max_steps: int, get_current_plan, get_last_task):
        self._anchor = anchor
        self._max_steps = max_steps
        self._get_current_plan = get_current_plan
        self._get_last_task = get_last_task

    async def __call__(self, payload: Any) -> Any:
        if self._anchor is None or not isinstance(payload, dict):
            return payload
        try:
            record = self._anchor.read() or ProgressRecord()
            tool_name = payload.get("tool", "")
            args = payload.get("args", {}) or {}
            error = payload.get("error")
            # Update current_task on first update (only if not set)
            if not record.current_task:
                last_task = self._get_last_task()
                if last_task:
                    record.current_task = last_task
            # Increment step counter from "N/M" pattern
            step_num = extract_step_num(record.current_step)
            step_num += 1
            record.current_step = f"{step_num}/{self._max_steps} (last: {tool_name})"
            # Update known_issues on failure
            if error:
                issue = f"{tool_name}: {str(error)[:120]}"
                if issue not in record.known_issues:
                    record.known_issues.append(issue)
            else:
                # Remove any existing entries that mention this tool
                # (recovery after a retry)
                record.known_issues = [
                    i for i in record.known_issues if not i.startswith(f"{tool_name}:")
                ]
            # Update next_step: best-effort from plan; else keep existing
            plan = self._get_current_plan()
            if plan is not None and hasattr(plan, "current_step"):
                try:
                    nxt = plan.current_step()
                    if nxt is not None and getattr(nxt, "description", None):
                        record.next_step = nxt.description
                except Exception:
                    pass
            # Update chain hash: H(prev_hash, op_str)
            try:
                op_str = f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"
            except Exception:
                op_str = f"{tool_name}:{str(args)[:200]}"
            from ..core.progress_anchor import ProgressAnchor

            record.op_hash = ProgressAnchor.compute_hash(record.op_hash, op_str)
            record.updated_at = datetime.now().isoformat()
            self._anchor.write(record)
        except Exception:
            pass
        return payload
