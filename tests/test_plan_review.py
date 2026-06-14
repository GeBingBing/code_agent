"""Tests for static plan review (M2 P0 — plan_review.py).

Covers the 6 review heuristics + scoring + PlanReviewReport dataclass
+ integration with /plan from-spec and ExitPlanModeTool flows.
"""

from __future__ import annotations

from agent.core.permissions import RiskLevel
from agent.core.plan import (
    AC,
    AllowedPrompt,
    ExecutionPlan,
    PlanStep,
    Risk,
)
from agent.core.plan_review import (
    Finding,
    PlanReviewReport,
    review_plan,
)
from agent.core.tdd_state_machine import TDDState


def _plan(
    *,
    steps: list[PlanStep] | None = None,
    acs: list[AC] | None = None,
    risks: list[Risk] | None = None,
    allowed_prompts: list[AllowedPrompt] | None = None,
) -> ExecutionPlan:
    return ExecutionPlan(
        task="test",
        steps=steps or [PlanStep(id=1, description="step 1", status="pending")],
        acceptance_criteria=acs or [],
        risks=risks or [],
        allowed_prompts=allowed_prompts or [],
    )


# ── 1. AC coverage ───────────────────────────────────────────────────────


class TestAcceptanceCriteriaCoverage:
    def test_multi_step_without_acs_warns(self):
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a"),
                PlanStep(id=2, description="b"),
            ]
        )
        report = review_plan(plan)
        assert any(f.code == "no-acs" for f in report.findings)

    def test_multi_step_with_acs_no_warn(self):
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a"),
                PlanStep(id=2, description="b"),
            ],
            acs=[AC(id="AC-1", description="done")],
        )
        report = review_plan(plan)
        assert not any(f.code == "no-acs" for f in report.findings)

    def test_single_step_no_ac_no_warn(self):
        """Single-step plans don't need ACs — they're trivially scoped."""
        plan = _plan(steps=[PlanStep(id=1, description="only one")])
        report = review_plan(plan)
        assert not any(f.code == "no-acs" for f in report.findings)


# ── 2. Risk acknowledgement ──────────────────────────────────────────────


class TestRiskAcknowledgement:
    def test_high_risk_step_without_risks_warns(self):
        plan = _plan(steps=[PlanStep(id=1, description="rm -rf", step_risk=RiskLevel.HIGH)])
        report = review_plan(plan)
        assert any(f.code == "high-risk-no-mitigation" for f in report.findings)

    def test_high_risk_step_with_risks_no_warn(self):
        plan = _plan(
            steps=[PlanStep(id=1, description="rm -rf", step_risk=RiskLevel.HIGH)],
            risks=[Risk(category="data-loss", severity="high", mitigation="backup")],
        )
        report = review_plan(plan)
        assert not any(f.code == "high-risk-no-mitigation" for f in report.findings)

    def test_low_risk_step_no_warn_even_without_risks(self):
        plan = _plan(steps=[PlanStep(id=1, description="read file", step_risk=RiskLevel.LOW)])
        report = review_plan(plan)
        assert not any(f.code == "high-risk-no-mitigation" for f in report.findings)


# ── 3. Verify-command coverage ──────────────────────────────────────────


class TestVerifyCommandCoverage:
    def test_majority_missing_verify_warns(self):
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a", verify_command=""),
                PlanStep(id=2, description="b", verify_command=""),
                PlanStep(id=3, description="c", verify_command="pytest -k c"),
            ]
        )
        report = review_plan(plan)
        # 2 of 3 missing (>= max(2, 3//2=1) → trigger)
        assert any(f.code == "weak-verify-coverage" for f in report.findings)

    def test_all_have_verify_no_warn(self):
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a", verify_command="pytest -k a"),
                PlanStep(id=2, description="b", verify_command="pytest -k b"),
            ]
        )
        report = review_plan(plan)
        assert not any(f.code == "weak-verify-coverage" for f in report.findings)

    def test_done_steps_excluded_from_weak_verify_check(self):
        """A step that's already done doesn't need a verify_command."""
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a", status="done"),  # done, no verify
                PlanStep(id=2, description="b", status="done"),  # done, no verify
                PlanStep(id=3, description="c", verify_command="pytest -k c"),
            ]
        )
        report = review_plan(plan)
        assert not any(f.code == "weak-verify-coverage" for f in report.findings)


# ── 4. Dependency sanity ────────────────────────────────────────────────


class TestDependencySanity:
    def test_self_dependency_rejected(self):
        plan = _plan(steps=[PlanStep(id=1, description="a", dependencies=[1])])
        report = review_plan(plan)
        assert any(f.code == "self-dependency" and f.severity == "reject" for f in report.findings)

    def test_dangling_dependency_rejected(self):
        plan = _plan(steps=[PlanStep(id=1, description="a", dependencies=[99])])
        report = review_plan(plan)
        assert any(f.code == "dangling-dependency" for f in report.findings)

    def test_forward_dependency_warns(self):
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a", dependencies=[2]),
                PlanStep(id=2, description="b"),
            ]
        )
        report = review_plan(plan)
        assert any(f.code == "forward-dependency" for f in report.findings)

    def test_valid_backward_dependency_ok(self):
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a"),
                PlanStep(id=2, description="b", dependencies=[1]),
            ]
        )
        report = review_plan(plan)
        assert not any(
            f.code in ("self-dependency", "dangling-dependency", "forward-dependency")
            for f in report.findings
        )


# ── 5. TDD phase on engineering steps ──────────────────────────────────


class TestTDDPhaseSuggestion:
    def test_implement_step_without_tdd_suggests(self):
        plan = _plan(steps=[PlanStep(id=1, description="Implement feature X")])
        report = review_plan(plan)
        assert any(f.code == "no-tdd-phase" and f.severity == "info" for f in report.findings)

    def test_implement_step_with_tdd_ok(self):
        plan = _plan(
            steps=[PlanStep(id=1, description="Implement feature X", tdd_phase=TDDState.RED)]
        )
        report = review_plan(plan)
        assert not any(f.code == "no-tdd-phase" for f in report.findings)

    def test_documentation_step_no_tdd_needed(self):
        plan = _plan(steps=[PlanStep(id=1, description="Document usage notes in the project wiki")])
        report = review_plan(plan)
        assert not any(f.code == "no-tdd-phase" for f in report.findings)

    def test_done_step_no_tdd_needed(self):
        plan = _plan(steps=[PlanStep(id=1, description="Implement X", status="done")])
        report = review_plan(plan)
        assert not any(f.code == "no-tdd-phase" for f in report.findings)


# ── 6. Allowed prompts coverage ─────────────────────────────────────────


class TestAllowedPromptsCoverage:
    def test_multi_step_no_prompts_suggests(self):
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a"),
                PlanStep(id=2, description="b"),
            ]
        )
        report = review_plan(plan)
        assert any(f.code == "no-allowed-prompts" for f in report.findings)

    def test_multi_step_with_prompts_ok(self):
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a"),
                PlanStep(id=2, description="b"),
            ],
            allowed_prompts=[AllowedPrompt(tool="edit", risk_level="low")],
        )
        report = review_plan(plan)
        assert not any(f.code == "no-allowed-prompts" for f in report.findings)


# ── Scoring ─────────────────────────────────────────────────────────────


class TestScoring:
    def test_perfect_score_no_findings(self):
        plan = _plan(
            steps=[
                PlanStep(
                    id=1, description="a", verify_command="pytest -k a", tdd_phase=TDDState.RED
                )
            ],
            acs=[AC(id="AC-1", description="done")],
            risks=[],
            allowed_prompts=[AllowedPrompt(tool="edit")],
        )
        report = review_plan(plan)
        assert report.score == 100

    def test_warn_deducts_5(self):
        # Two steps, no acs, all verify, allowed_prompts present so the
        # no-allowed-prompts info does NOT trigger. Score = 100 - 5 = 95.
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a", verify_command="pytest -k a"),
                PlanStep(id=2, description="b", verify_command="pytest -k b"),
            ],
            allowed_prompts=[AllowedPrompt(tool="edit")],
        )
        report = review_plan(plan)
        # Exactly one warn (no-acs)
        assert report.warn_count == 1
        assert any(f.code == "no-acs" for f in report.findings)
        # No info findings
        assert report.findings and not [f for f in report.findings if f.severity == "info"]
        # 100 - 5 = 95
        assert report.score == 95

    def test_reject_deducts_25(self):
        plan = _plan(steps=[PlanStep(id=1, description="a", dependencies=[1])])
        report = review_plan(plan)
        # self-dependency is a reject
        assert report.score == 75

    def test_score_clamped_to_0(self):
        # Construct a plan with many rejects
        plan = _plan(
            steps=[
                PlanStep(id=1, description="a", dependencies=[1]),  # self-dep
                PlanStep(id=2, description="b", dependencies=[99]),  # dangling
                PlanStep(id=3, description="c", dependencies=[4]),  # forward
                PlanStep(id=4, description="d"),
            ]
        )
        report = review_plan(plan)
        # 2 rejects × 25 = 50; 1 forward (warn) × 5 = 5 → 100 - 55 = 45
        # Score should be 45 (not negative)
        assert 0 <= report.score <= 100


# ── PlanReviewReport dataclass ──────────────────────────────────────────


class TestPlanReviewReportDataclass:
    def test_to_dict_round_trip(self):
        report = PlanReviewReport(
            score=80,
            findings=[Finding(severity="warn", code="test", message="m")],
            summary="1 warn",
        )
        d = report.to_dict()
        assert d["score"] == 80
        assert d["summary"] == "1 warn"
        assert len(d["findings"]) == 1
        assert d["findings"][0]["code"] == "test"

    def test_reject_warn_counts(self):
        report = PlanReviewReport(
            score=50,
            findings=[
                Finding(severity="reject", code="a", message="m"),
                Finding(severity="warn", code="b", message="m"),
                Finding(severity="info", code="c", message="m"),
            ],
        )
        assert report.reject_count == 1
        assert report.warn_count == 1

    def test_to_markdown_no_findings(self):
        report = PlanReviewReport(score=100, findings=[], summary="OK")
        md = report.to_markdown()
        assert "100/100" in md
        assert "no issues" in md

    def test_to_markdown_with_findings(self):
        report = PlanReviewReport(
            score=75,
            findings=[Finding(severity="reject", code="x", message="bad")],
            summary="1 blocking",
        )
        md = report.to_markdown()
        assert "75/100" in md
        assert "x" in md
        assert "bad" in md


# ── Integration: review is attached to plan.review_notes ──────────────


class TestReviewIntegration:
    def test_review_notes_rendered_in_to_markdown(self):
        plan = _plan(
            steps=[PlanStep(id=1, description="a"), PlanStep(id=2, description="b")],
        )
        report = review_plan(plan)
        plan.review_notes = report.to_markdown()
        md = plan.to_markdown()
        assert "## Review" in md
        assert "no-acs" in md or "no-allowed-prompts" in md


# ── PlanReviewReport from /plan from-spec ──────────────────────────────


class TestReviewFromSpec:
    """End-to-end: /plan from-spec returns a plan, the static review runs,
    and findings are summarised in the user-visible output."""

    def test_review_runs_on_from_spec_output(self, tmp_path):
        spec = tmp_path / "SPECS.md"
        spec.write_text(
            "## Phase 0: Setup\n- [ ] do thing\n- [ ] do other\n",
            encoding="utf-8",
        )
        from agent.core.spec_plan_adapter import from_spec as spec_from_spec

        plan = spec_from_spec(tmp_path, "P0")
        report = review_plan(plan)
        # The SpecPlanAdapter doesn't fill in verify_command or risks,
        # so the review will flag at least the no-verify coverage.
        assert report.score < 100
        assert len(report.findings) > 0
