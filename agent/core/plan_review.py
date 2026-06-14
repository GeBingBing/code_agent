"""Static plan review — pre-execution sanity check (M2 P0).

Before a plan is approved (via ``/plan from-spec`` or
``ExitPlanModeTool``), we run a fast, deterministic review that catches
the most common plan-quality issues without paying the LLM cost of a
full ``DualReviewManager`` review.

Why static, not LLM-based?
  * LLM dual-review costs two round-trips per plan.  For a slash command
    like ``/plan from-spec P0`` we want a sub-second response.
  * The M3 ``/plan refine`` command will chain the LLM reviewer AFTER
    the user has eyeballed the static report.  M2 is the cheap
    safety-net.
  * The static check is exhaustive for the small set of issues we can
    detect from the plan data alone (AC coverage, risk acknowledgement,
    verify-command presence, dependency correctness).

What it checks
--------------
1. **Acceptance criteria coverage** — at least one AC on a multi-step plan.
2. **Risk acknowledgement** — risky plans (steps with ``step_risk in
   {HIGH, CRITICAL}``) must declare at least one risk.
3. **Verify-command coverage** — most steps should have a
   ``verify_command`` (M2 schema field).
4. **Dependency sanity** — every step id in ``dependencies`` must exist
   and must reference a strictly-earlier step (no forward refs, no
   self-refs).
5. **TDD phase on engineering steps** — if a step is likely
   code-changing (heuristic: description contains verbs like
   "implement", "fix", "add", "refactor") and has no ``tdd_phase``, warn.
6. **Allowed prompts coverage** — multi-step plans should declare at
   least one allowed action so the user-approval dialog is meaningful.

Output
------
A ``PlanReviewReport`` dataclass:
  * ``score`` — integer 0..100 (higher is better)
  * ``findings`` — list of ``Finding`` (severity + message + location)
  * ``summary`` — one-line summary suitable for ``/plan show`` output

The report is also serialised into the plan's markdown via
``plan.review_notes`` so it appears whenever the plan is rendered.
Optionally persisted to ``~/.coding-agent/plans/<plan_id>.review.md``
for archival.

Integration points
-------------------
The report is attached to the plan as ``plan.review_notes`` (M2 field).
Callers (``/plan from-spec``, ``ExitPlanModeTool.execute``) decide
whether to BLOCK on REJECT findings or merely surface them.  M2 P0
default is "surface only" — the user retains final say.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .plan import ExecutionPlan, PlanStep


@dataclass
class Finding:
    """A single review observation."""

    severity: str  # "info" | "warn" | "reject"
    code: str  # short stable code, e.g. "no-acs"
    message: str
    step_id: Optional[int] = None  # if the finding is per-step


@dataclass
class PlanReviewReport:
    """Outcome of a static plan review."""

    score: int  # 0..100
    findings: List[Finding] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "summary": self.summary,
            "findings": [
                {
                    "severity": f.severity,
                    "code": f.code,
                    "message": f.message,
                    "step_id": f.step_id,
                }
                for f in self.findings
            ],
        }

    @property
    def reject_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "reject")

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warn")

    def to_markdown(self) -> str:
        """Render as a markdown report for the user."""
        if not self.findings:
            return f"## Review: {self.score}/100 — no issues found."
        lines = [f"## Review: {self.score}/100"]
        for f in self.findings:
            icon = {"info": "i", "warn": "!", "reject": "✗"}.get(f.severity, "?")
            location = f" (step {f.step_id})" if f.step_id is not None else ""
            lines.append(f"- [{icon}] `{f.code}`{location} — {f.message}")
        return "\n".join(lines)


# ── Heuristics ──────────────────────────────────────────────────────────


_VERBS_SUGGESTING_CODE_CHANGE = (
    "implement",
    "fix",
    "add ",
    "refactor",
    "change",
    "rewrite",
    "replace",
    "update ",
    "introduce",
    "migrate",
)


def _likely_code_changing(step: PlanStep) -> bool:
    desc = step.description.lower()
    return any(v in desc for v in _VERBS_SUGGESTING_CODE_CHANGE)


def _has_high_risk_step(plan: ExecutionPlan) -> bool:
    """True if any step is flagged HIGH or CRITICAL risk."""
    from .permissions import RiskLevel

    for s in plan.steps:
        if s.step_risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return True
    return False


# ── Public API ──────────────────────────────────────────────────────────


def review_plan(plan: ExecutionPlan) -> PlanReviewReport:
    """Run the static plan review and return a report.

    The plan is NOT mutated.  Callers are expected to attach the report
    to ``plan.review_notes`` (or persist it separately) themselves.
    """
    findings: List[Finding] = []

    # 1. AC coverage
    if len(plan.steps) > 1 and not plan.acceptance_criteria:
        findings.append(
            Finding(
                severity="warn",
                code="no-acs",
                message=(
                    f"Plan has {len(plan.steps)} steps but no acceptance criteria. "
                    "Consider adding ## Acceptance Criteria so the executor "
                    "knows what 'done' means."
                ),
            )
        )

    # 2. Risk acknowledgement
    if _has_high_risk_step(plan) and not plan.risks:
        findings.append(
            Finding(
                severity="warn",
                code="high-risk-no-mitigation",
                message=(
                    "Plan has HIGH/CRITICAL risk steps but no ## Risks section. "
                    "Document mitigations so reviewers can sign off."
                ),
            )
        )

    # 3. Verify-command coverage (per step)
    no_verify_steps = [s for s in plan.steps if s.status != "done" and not s.verify_command.strip()]
    if len(no_verify_steps) >= max(2, len(plan.steps) // 2):
        findings.append(
            Finding(
                severity="warn",
                code="weak-verify-coverage",
                message=(
                    f"{len(no_verify_steps)} of {len(plan.steps)} steps lack a "
                    "verify_command. Add one (e.g. `pytest -k <step>`) so the "
                    "executor can self-verify."
                ),
            )
        )

    # 4. Dependency sanity
    step_ids = {s.id for s in plan.steps}
    for s in plan.steps:
        for dep in s.dependencies:
            if dep == s.id:
                findings.append(
                    Finding(
                        severity="reject",
                        code="self-dependency",
                        message=f"Step {s.id} depends on itself.",
                        step_id=s.id,
                    )
                )
            elif dep not in step_ids:
                findings.append(
                    Finding(
                        severity="reject",
                        code="dangling-dependency",
                        message=(
                            f"Step {s.id} depends on missing step #{dep}. "
                            f"Valid ids: {sorted(step_ids)}"
                        ),
                        step_id=s.id,
                    )
                )
            elif dep >= s.id:
                # Forward reference or same-id (excluding self which is caught above)
                findings.append(
                    Finding(
                        severity="warn",
                        code="forward-dependency",
                        message=(
                            f"Step {s.id} depends on a later step (#{dep}). "
                            "Dependencies should reference earlier steps."
                        ),
                        step_id=s.id,
                    )
                )

    # 5. TDD phase on engineering steps
    for s in plan.steps:
        if s.status == "done":
            continue
        if s.tdd_phase is None and _likely_code_changing(s):
            findings.append(
                Finding(
                    severity="info",
                    code="no-tdd-phase",
                    message=(
                        f"Step {s.id} looks like a code change but has no "
                        "tdd_phase. Add `tdd_phase=RED` if the project "
                        "follows TDD (PR-02)."
                    ),
                    step_id=s.id,
                )
            )

    # 6. Allowed-prompt coverage
    if len(plan.steps) > 1 and not plan.allowed_prompts:
        findings.append(
            Finding(
                severity="info",
                code="no-allowed-prompts",
                message=(
                    "Multi-step plan has no ## Allowed Actions section. "
                    "Document which tool categories the executor needs so "
                    "the approval dialog is meaningful."
                ),
            )
        )

    # ── Score ───────────────────────────────────────────────────────
    # Start at 100, deduct per finding (weighted by severity).
    score = 100
    for f in findings:
        if f.severity == "reject":
            score -= 25
        elif f.severity == "warn":
            score -= 5
        else:  # info
            score -= 1
    score = max(0, min(100, score))

    # Summary line
    if not findings:
        summary = "No issues found."
    else:
        parts = []
        n_reject = sum(1 for f in findings if f.severity == "reject")
        n_warn = sum(1 for f in findings if f.severity == "warn")
        n_info = sum(1 for f in findings if f.severity == "info")
        if n_reject:
            parts.append(f"{n_reject} blocking")
        if n_warn:
            parts.append(f"{n_warn} warnings")
        if n_info:
            parts.append(f"{n_info} info")
        summary = ", ".join(parts) + f" — score {score}/100"

    return PlanReviewReport(score=score, findings=findings, summary=summary)
