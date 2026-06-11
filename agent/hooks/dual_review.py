"""Dual-agent review hook (PR-19, extracted from AgentEngine).

Runs on BEFORE_TOOL_EXECUTION. If the tool is high-risk, dispatches
two parallel reviewers via DualReviewManager. Verdict translation:
  - REJECT  → raise PermissionDenied (engine surfaces as tool error)
  - ABSTAIN → raise ReviewRequiresUser (user must adjudicate)
  - APPROVE → return payload unchanged

Decision is also logged to the audit log regardless of outcome
(defense in depth). Originally `AgentEngine._dual_review_hook`.
"""

from __future__ import annotations

from typing import Any

from ..core.dual_review import (
    PermissionDenied,
    ReviewRequiresUser,
)


class DualReviewHook:
    """Run two parallel reviewers on high-risk tool calls.

    Constructor takes callables (getters) for the runtime-mutable
    dependencies: `get_dual_review`, `get_audit`, `get_trace_id`.
    Using getters (not direct references) lets callers mutate
    `engine.dual_review` / `engine.audit` after construction and
    have the hook pick up the new values on the next call —
    preserving the behavior of the pre-PR-19 inline hook.
    """

    def __init__(
        self,
        get_dual_review,
        get_audit=lambda: None,
        get_trace_id=lambda: "",
    ):
        self._get_dual_review = get_dual_review
        self._get_audit = get_audit
        self._get_trace_id = get_trace_id

    async def __call__(self, payload: Any) -> Any:
        dual_review = self._get_dual_review()
        if dual_review is None or not isinstance(payload, dict):
            return payload
        tool_name = payload.get("tool", "")
        if not dual_review.is_high_risk(tool_name):
            return payload
        # Note: we don't pre-check `permissions.check()` here because the
        # engine runs that *after* this hook. If permissions would have
        # rejected, the dual-review was a small extra cost — the cheaper
        # pattern matching in `check()` runs later as a second line of
        # defense. Defense in depth, not duplication.
        args = payload.get("args", {}) or {}
        context = payload.get("context", "") or ""
        result = await dual_review.review(tool_name, args, context=context)
        # Always log the decision to audit, regardless of outcome
        audit = self._get_audit()
        if audit is not None:
            try:
                audit.log(
                    {
                        "session_id": self._get_trace_id(),
                        "agent_id": "main",
                        "action": "dual_review",
                        "tool": tool_name,
                        "metadata": {
                            "decisions": [
                                {
                                    "reviewer": d.reviewer_id,
                                    "model": d.model,
                                    "verdict": d.verdict.value,
                                    "rationale": d.rationale[:200],
                                    "elapsed_ms": d.elapsed_ms,
                                }
                                for d in result.decisions
                            ],
                            "final_verdict": result.final_verdict.value,
                            "consensus": result.consensus,
                            "requires_user": result.requires_user,
                        },
                    }
                )
            except Exception:
                pass  # Audit must never break tool execution
        # Translate verdict into a hook outcome
        try:
            dual_review.review_decision(result)
        except PermissionDenied:
            raise
        except ReviewRequiresUser:
            raise
        return payload
