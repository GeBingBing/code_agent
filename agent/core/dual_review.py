"""Dual-agent review for high-risk tool calls (PR-11).

Why dual-agent review?
- A single LLM may miss subtle risks (e.g., `rm -rf` chained with `&&`).
- Two *different* models reviewing the same call reduces single-model bias.
- Disagreement surfaces to the user, who has the final say.

Design (per docs/1.md §8 + docs/参考.md 纵深防御):
- HIGH_RISK_TOOLS: write_file, apply_diff, execute_command, git push,
  create_pr, web_fetch, install_package, uninstall_package.
- Two reviewers (primary + secondary) are invoked **in parallel** via
  asyncio.gather. Each gets the same prompt and returns a verdict.
- Aggregation rules:
    * Any REJECT  → final REJECT (engine blocks the tool call)
    * All APPROVE  → final APPROVE
    * Otherwise    → ABSTAIN (requires_user=True, surfaced to CLI)
- Rate limit: max 5 calls per minute (anti-abuse guard).
- Privacy: args are summarized — full paths/commands/URLs visible to
  the LLM reviewers, but never persisted in audit (only the verdict
  + rationale are logged via the engine's existing audit hook).

This module is *pluggable*: callers can supply their own `primary_chat`
and `secondary_chat` coroutines to integrate with their preferred
LLM clients. If only one chat fn is supplied, the manager creates a
secondary that uses a different model name as a stub.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, List, Optional

# ── Public exceptions ───────────────────────────────────────────────


class PermissionDenied(Exception):
    """Raised when a high-risk tool call is rejected by dual review.

    The engine catches this on the BEFORE_TOOL_EXECUTION hook and
    surfaces it as a tool error to the LLM, so the model can adapt.
    """

    def __init__(self, message: str, decisions: Optional[list] = None):
        super().__init__(message)
        self.decisions = decisions or []


class ReviewRequiresUser(Exception):
    """Raised when reviewers disagree — user must adjudicate.

    Distinct from PermissionDenied so the engine can route the two
    outcomes differently (e.g. CLI shows a "Override and proceed /
    Abort" panel rather than just a hard block).
    """

    def __init__(self, message: str, result: Optional["DualReviewResult"] = None):
        super().__init__(message)
        self.result = result


# ── Verdict + decision data classes ──────────────────────────────────


class ReviewVerdict(Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"


@dataclass
class ReviewDecision:
    reviewer_id: str
    model: str
    verdict: ReviewVerdict
    rationale: str
    elapsed_ms: float = 0.0


@dataclass
class DualReviewResult:
    decisions: List[ReviewDecision]
    final_verdict: ReviewVerdict
    requires_user: bool
    consensus: bool
    tool: str = ""
    args: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "args": self.args,
            "final_verdict": self.final_verdict.value,
            "requires_user": self.requires_user,
            "consensus": self.consensus,
            "decisions": [
                {
                    "reviewer_id": d.reviewer_id,
                    "model": d.model,
                    "verdict": d.verdict.value,
                    "rationale": d.rationale,
                    "elapsed_ms": d.elapsed_ms,
                }
                for d in self.decisions
            ],
        }


# ── Rate limiter ────────────────────────────────────────────────────


class RateLimiter:
    """Sliding-window rate limiter. Default: 5 calls / 60s."""

    def __init__(self, max_per_minute: int = 5, window_seconds: int = 60):
        self.max = max_per_minute
        self.window = window_seconds
        self._calls: list = []

    def allow(self) -> bool:
        now = time.time()
        cutoff = now - self.window
        # Drop expired
        self._calls = [t for t in self._calls if t > cutoff]
        if len(self._calls) >= self.max:
            return False
        self._calls.append(now)
        return True

    def reset(self) -> None:
        self._calls = []

    def used(self) -> int:
        return len(self._calls)


# ── Default reviewer prompt ─────────────────────────────────────────

_REVIEW_PROMPT_TEMPLATE = """\
You are an independent code review agent. Evaluate the following \
high-risk tool call for safety and correctness.

Tool: {tool}
Args: {args}
Context: {context}

Consider the following:
- Could this cause data loss (e.g. rm -rf, dropping tables, overwriting files)?
- Could this leak secrets (env vars, credentials, tokens, API keys)?
- Could this violate security policies (SQL injection, XSS, RCE)?
- Is the path/target within expected scope (e.g. project root, not system dirs)?
- Is the action reversible? If not, that's an additional risk factor.

Respond with strict JSON (no prose, no code fences) in exactly this form:
{{
  "verdict": "approve" | "reject",
  "rationale": "1-2 sentence explanation"
}}
"""


# ── Manager ─────────────────────────────────────────────────────────


# A chat callable: takes a list[Message], returns (text_response, usage_dict|None)
ChatFn = Callable[[List, bool], Awaitable[tuple]]


class DualReviewManager:
    """Manages parallel two-agent review of high-risk tool calls.

    Constructor takes *named* chat callables so tests can inject mocks
    easily. `primary_chat` and `secondary_chat` follow the same signature
    as `LLMClient.chat(messages, stream) -> (str, usage_dict_or_None)`.
    """

    HIGH_RISK_TOOLS = frozenset(
        {
            "write_file",
            "apply_diff",
            "edit_file",
            "insert_after_line",
            "replace_lines",
            "execute_command",
            "git",
            "git_push",  # may not exist as a tool name; tolerated
            "create_pr",
            "web_fetch",
            "web_search",
            "install_package",
            "uninstall_package",
            "smart_commit",
            "smart_branch",
            "sandbox_execute",
        }
    )

    def __init__(
        self,
        primary_chat: Optional[ChatFn] = None,
        secondary_chat: Optional[ChatFn] = None,
        primary_model: str = "primary",
        secondary_model: str = "secondary",
        rate_limiter: Optional[RateLimiter] = None,
        max_per_minute: int = 5,
    ):
        self.primary_chat = primary_chat
        self.secondary_chat = secondary_chat or self._stub_secondary
        self.primary_model = primary_model
        self.secondary_model = secondary_model
        self.rate_limiter = rate_limiter or RateLimiter(max_per_minute=max_per_minute)
        # Stats — useful for /status and tests
        self.reviews_run: int = 0
        self.reviews_approved: int = 0
        self.reviews_rejected: int = 0
        self.reviews_user_required: int = 0
        self.reviews_rate_limited: int = 0

    # ── Public API ──────────────────────────────────────────────

    def is_high_risk(self, tool_name: str) -> bool:
        return tool_name in self.HIGH_RISK_TOOLS

    async def review(
        self,
        tool_name: str,
        args: dict,
        context: str = "",
    ) -> DualReviewResult:
        """Run two reviewers in parallel and aggregate their decisions."""
        # Rate limit — if exceeded, force user adjudication (safer than auto-approve)
        if not self.rate_limiter.allow():
            self.reviews_rate_limited += 1
            self.reviews_user_required += 1
            return DualReviewResult(
                decisions=[],
                final_verdict=ReviewVerdict.ABSTAIN,
                requires_user=True,
                consensus=False,
                tool=tool_name,
                args=args,
            )

        prompt = self._build_review_prompt(tool_name, args, context)
        self.reviews_run += 1

        # Run both reviewers concurrently
        decisions = await asyncio.gather(
            self._review_with(self.primary_chat, prompt, "primary", self.primary_model),
            self._review_with(self.secondary_chat, prompt, "secondary", self.secondary_model),
            return_exceptions=False,
        )
        # asyncio.gather propagates exceptions; we caught them inside _review_with
        # and returned ABSTAIN. So `decisions` is a clean list[ReviewDecision].

        result = self._aggregate(decisions)
        result.tool = tool_name
        result.args = args

        # Stats
        if result.final_verdict == ReviewVerdict.APPROVE:
            self.reviews_approved += 1
        elif result.final_verdict == ReviewVerdict.REJECT:
            self.reviews_rejected += 1
        else:
            self.reviews_user_required += 1

        return result

    def review_decision(self, result: DualReviewResult) -> None:
        """Translate a DualReviewResult into a hook exception (or pass).

        Engine hooks call this to decide whether to raise.
        - REJECT  → raise PermissionDenied
        - ABSTAIN → raise ReviewRequiresUser
        - APPROVE → return (no raise)
        """
        if result.final_verdict == ReviewVerdict.REJECT:
            rationale = "; ".join(d.rationale for d in result.decisions) or "no rationale"
            raise PermissionDenied(
                f"Dual-agent review rejected: {rationale}",
                decisions=result.decisions,
            )
        if result.requires_user or result.final_verdict == ReviewVerdict.ABSTAIN:
            raise ReviewRequiresUser(
                "Dual-agent review split. Please review the decisions and confirm.",
                result=result,
            )

    # ── Internals ──────────────────────────────────────────────

    def _build_review_prompt(self, tool_name: str, args: dict, context: str) -> str:
        # Cap args size to avoid blowing prompt budget on large diffs
        try:
            args_str = json.dumps(args, default=str, ensure_ascii=False)
        except Exception:
            args_str = str(args)[:2000]
        if len(args_str) > 2000:
            args_str = args_str[:2000] + "…(truncated)"
        return _REVIEW_PROMPT_TEMPLATE.format(
            tool=tool_name,
            args=args_str,
            context=(context or "(none)")[:1000],
        )

    async def _review_with(
        self,
        chat_fn: Optional[ChatFn],
        prompt: str,
        reviewer_id: str,
        model_name: str,
    ) -> ReviewDecision:
        start = time.time()
        if chat_fn is None:
            return ReviewDecision(
                reviewer_id=reviewer_id,
                model=model_name,
                verdict=ReviewVerdict.ABSTAIN,
                rationale="No reviewer configured (chat_fn is None)",
                elapsed_ms=(time.time() - start) * 1000.0,
            )
        try:
            # Lazy import Message to avoid circular imports
            from ..llm.client import Message

            messages = [Message(role="user", content=prompt)]
            resp, _usage = await chat_fn(messages, False)
        except Exception as e:
            return ReviewDecision(
                reviewer_id=reviewer_id,
                model=model_name,
                verdict=ReviewVerdict.ABSTAIN,
                rationale=f"Reviewer error: {type(e).__name__}: {e}",
                elapsed_ms=(time.time() - start) * 1000.0,
            )
        verdict, rationale = _parse_verdict_response(resp)
        return ReviewDecision(
            reviewer_id=reviewer_id,
            model=model_name,
            verdict=verdict,
            rationale=rationale,
            elapsed_ms=(time.time() - start) * 1000.0,
        )

    async def _stub_secondary(self, messages, stream: bool = False):
        """Fallback secondary reviewer — approves unless args are clearly destructive.

        Used when only primary is configured (e.g. test with single LLM).
        Inspects only the **args section** of the prompt, not the example text
        (otherwise the prompt template's own "rm -rf" example would always
        trigger a reject).
        """
        text = ""
        for m in messages or []:
            content = getattr(m, "content", "") or ""
            text += content + "\n"
        # Extract the args section (after "Args:") and inspect it
        args_section = ""
        marker = "Args: "
        idx = text.find(marker)
        if idx != -1:
            # Args section runs until "Context:" or end
            end = text.find("Context:", idx)
            args_section = text[idx + len(marker) : end if end != -1 else None]
        args_lower = args_section.lower()
        destructive = (
            "rm -rf" in args_lower
            or "drop table" in args_lower
            or "drop database" in args_lower
            or "mkfs" in args_lower
        )
        if destructive:
            resp = json.dumps(
                {
                    "verdict": "reject",
                    "rationale": "Destructive operation detected in args by stub reviewer",
                }
            )
        else:
            resp = json.dumps(
                {
                    "verdict": "approve",
                    "rationale": "Stub secondary reviewer: no destructive patterns in args",
                }
            )
        return resp, None

    @staticmethod
    def _aggregate(decisions: List[ReviewDecision]) -> DualReviewResult:
        """Aggregate reviewer decisions into a final verdict.

        Rules (in order):
            1. Any REJECT  → final REJECT (consensus=False, requires_user=False)
            2. All APPROVE → final APPROVE (consensus=True, requires_user=False)
            3. Otherwise   → ABSTAIN (consensus=False, requires_user=True)
        """
        verdicts = [d.verdict for d in decisions]
        approves = verdicts.count(ReviewVerdict.APPROVE)
        rejects = verdicts.count(ReviewVerdict.REJECT)
        # Edge case: empty decisions list → treat as ABSTAIN
        if not decisions:
            return DualReviewResult(
                decisions=decisions,
                final_verdict=ReviewVerdict.ABSTAIN,
                requires_user=True,
                consensus=False,
            )
        if rejects >= 1:
            return DualReviewResult(
                decisions=decisions,
                final_verdict=ReviewVerdict.REJECT,
                requires_user=False,
                consensus=(rejects == len(decisions)),
            )
        if approves == len(decisions):
            return DualReviewResult(
                decisions=decisions,
                final_verdict=ReviewVerdict.APPROVE,
                requires_user=False,
                consensus=True,
            )
        # At least one ABSTAIN (or split) → user must adjudicate
        return DualReviewResult(
            decisions=decisions,
            final_verdict=ReviewVerdict.ABSTAIN,
            requires_user=True,
            consensus=False,
        )


# ── Response parsing ────────────────────────────────────────────────


def _parse_verdict_response(resp: str) -> tuple:
    """Extract (verdict, rationale) from reviewer JSON response.

    Tolerant of:
    - Markdown code fences (```json ... ```)
    - Smart quotes around JSON keys
    - Prose before/after the JSON object
    - Missing rationale (defaults to empty string)
    - Lowercase / uppercase / whitespace variations of "approve"/"reject"

    PR-16: JSON parsing delegated to LLMExtractor._safe_json_loads
    (the shared tolerant parser). Falls back to (ABSTAIN, "Could not
    parse: …") on any failure so a broken reviewer response is never
    a security risk (it just defers to the user).
    """
    if not isinstance(resp, str):
        return ReviewVerdict.ABSTAIN, "Non-string response"
    text = resp.strip()
    from .llm_extractor import LLMExtractor

    parsed = LLMExtractor._safe_json_loads(text)
    if not isinstance(parsed, dict):
        return ReviewVerdict.ABSTAIN, f"Could not parse: {text[:80]}"
    raw_verdict = str(parsed.get("verdict", "")).strip().lower()
    rationale = str(parsed.get("rationale", "")).strip()
    if raw_verdict in ("approve", "approved", "yes", "y", "ok", "allow"):
        return ReviewVerdict.APPROVE, rationale
    if raw_verdict in ("reject", "rejected", "no", "n", "deny", "denied", "block"):
        return ReviewVerdict.REJECT, rationale
    return ReviewVerdict.ABSTAIN, f"Unknown verdict: {raw_verdict!r}"


# ── Singleton ───────────────────────────────────────────────────────

_default_manager: Optional[DualReviewManager] = None


def get_dual_review_manager(
    primary_chat: Optional[ChatFn] = None,
    secondary_chat: Optional[ChatFn] = None,
    primary_model: str = "primary",
    secondary_model: str = "secondary",
) -> DualReviewManager:
    """Return the process-wide DualReviewManager (lazy-initialized)."""
    global _default_manager
    if _default_manager is None:
        _default_manager = DualReviewManager(
            primary_chat=primary_chat,
            secondary_chat=secondary_chat,
            primary_model=primary_model,
            secondary_model=secondary_model,
        )
    return _default_manager


def reset_dual_review_manager() -> None:
    """Drop the singleton — used in tests."""
    global _default_manager
    _default_manager = None
