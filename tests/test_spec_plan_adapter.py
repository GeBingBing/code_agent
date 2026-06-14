"""Tests for SpecPlanAdapter (M2 P0).

These pin down the bridge from SPECS.md → ExecutionPlan:
  * 1:1 mapping of acceptance_criteria → PlanStep (with status='done'
    preserved for already-done ACs)
  * raw_tasks fallback when a phase has no ACs
  * Empty-phase placeholder step
  * Stable ``spec-<phase_id>-<ts>-<uuid>`` plan id
  * SpecPlanAdapterError when phase_id is not found
  * list_eligible_phases filters out completed/backlog phases
  * tdd_phase default (RED) override via kwarg
  * parent_plan_id round-trip for refinement chains
  * The adapter does NOT mutate SPECS.md or call mark_ac_done
  * Allowed-prompt seed list covers obvious tools
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.core.plan import ExecutionPlan, PlanStep
from agent.core.spec_plan_adapter import (
    SpecPlanAdapterError,
    from_spec,
    list_eligible_phases,
)
from agent.core.tdd_state_machine import TDDState

# ── Fixtures ──────────────────────────────────────────────────────────────


SAMPLE_SPEC_MD = """\
# Spec

## Phase 0: Setup
- [x] Initialise project structure
- [x] Install pytest
- [ ] Add CI workflow

## Phase 1: Implementation
- [ ] Implement feature A
- [ ] Implement feature B with extensive edge case handling and complex validation logic that goes on
- [ ] Write tests for A and B
"""


@pytest.fixture
def workspace_with_spec(tmp_path):
    """Workspace with a small SPECS.md covering 2 phases."""
    spec = tmp_path / "SPECS.md"
    spec.write_text(SAMPLE_SPEC_MD, encoding="utf-8")
    return tmp_path


# ── list_eligible_phases ───────────────────────────────────────────────────


class TestListEligiblePhases:
    def test_includes_partial_and_planned(self, workspace_with_spec):
        phases = list_eligible_phases(workspace_with_spec)
        # Both phases lack an explicit status emoji, so spec_loader
        # defaults to "planned" — both are eligible. (P0's ACs being all
        # done does not flip the phase status; that's a future-AC-tracker
        # concern, not a plan-eligibility concern.)
        assert "P1" in phases
        assert "P0" in phases

    def test_completed_phase_excluded(self, tmp_path):
        """A phase marked with ✅ should be excluded — it's read-only."""
        spec = tmp_path / "SPECS.md"
        spec.write_text(
            "## Phase 0: Done ✅\n- [x] done thing\n" "## Phase 1: Active 🔜\n- [ ] todo thing\n",
            encoding="utf-8",
        )
        phases = list_eligible_phases(tmp_path)
        assert "P0" not in phases
        assert "P1" in phases

    def test_returns_empty_when_no_spec(self, tmp_path):
        # No SPECS.md at all
        assert list_eligible_phases(tmp_path) == []


# ── from_spec — happy path ────────────────────────────────────────────────


class TestFromSpecHappyPath:
    def test_builds_plan_from_phase_with_pending_acs(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        assert isinstance(plan, ExecutionPlan)
        # 3 ACs in P1 → 3 steps
        assert len(plan.steps) == 3

    def test_step_descriptions_match_ac_ids(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        # ACs are auto-numbered P1-1, P1-2, P1-3
        for step, expected_id in zip(plan.steps, ["P1-1", "P1-2", "P1-3"]):
            assert (
                expected_id in step.description
            ), f"Step {step.id} should reference AC {expected_id}: {step.description}"

    def test_plan_id_format(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        # spec-P1-<ts>-<uuid>
        assert plan.plan_id.startswith("spec-P1-")
        # Suffix is ts-uuid (4 dash-separated parts)
        parts = plan.plan_id.split("-")
        assert len(parts) >= 4

    def test_plan_title_and_summary_match_phase(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        assert plan.title == "Implementation"
        assert plan.summary == "Implementation"

    def test_task_derived_from_phase(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        assert "P1" in plan.task
        assert "Implementation" in plan.task

    def test_acceptance_criteria_mirrored(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        assert len(plan.acceptance_criteria) == 3
        for ac, step in zip(plan.acceptance_criteria, plan.steps):
            # Each AC's id appears in the step description
            assert ac.id in step.description
            # Each AC has a description
            assert ac.description


# ── from_spec — TDD defaults and overrides ───────────────────────────────


class TestFromSpecTDDPhase:
    def test_default_tdd_phase_is_red(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        for step in plan.steps:
            assert step.tdd_phase == TDDState.RED

    def test_tdd_phase_can_be_disabled(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1", step_tdd_phase=None)
        for step in plan.steps:
            assert step.tdd_phase is None

    def test_tdd_phase_can_be_set_to_green(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1", step_tdd_phase=TDDState.GREEN)
        for step in plan.steps:
            assert step.tdd_phase == TDDState.GREEN


# ── from_spec — done ACs preserved ──────────────────────────────────────


class TestFromSpecPreservesDoneACs:
    def test_done_acs_become_done_steps(self, workspace_with_spec):
        # P0 has 2 done + 1 pending AC
        plan = from_spec(workspace_with_spec, "P0")
        assert len(plan.steps) == 3
        # First two are done
        assert plan.steps[0].status == "done"
        assert plan.steps[1].status == "done"
        # Last is pending
        assert plan.steps[2].status == "pending"


# ── from_spec — error paths ──────────────────────────────────────────────


class TestFromSpecErrors:
    def test_unknown_phase_raises(self, workspace_with_spec):
        with pytest.raises(SpecPlanAdapterError) as ei:
            from_spec(workspace_with_spec, "P99")
        assert "P99" in str(ei.value)
        # Error mentions available phases for hint
        assert "P0" in str(ei.value) or "P1" in str(ei.value)

    def test_no_spec_file_yields_empty_phases(self, tmp_path):
        with pytest.raises(SpecPlanAdapterError):
            from_spec(tmp_path, "P0")


# ── from_spec — parent_plan_id and allowed_prompts ───────────────────────


class TestFromSpecMetadata:
    def test_parent_plan_id_round_trips(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1", parent_plan_id="spec-P0-1700000000-abc123")
        assert plan.parent_plan_id == "spec-P0-1700000000-abc123"

    def test_default_parent_plan_id_empty(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        assert plan.parent_plan_id == ""

    def test_allowed_prompts_seeded(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        tools = {ap.tool for ap in plan.allowed_prompts}
        # Seeded with edit + run_tests
        assert "edit" in tools
        assert "run_tests" in tools

    def test_revisions_default_to_one(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        assert plan.revision == 1


# ── from_spec — fallbacks ───────────────────────────────────────────────


class TestFromSpecFallbacks:
    def test_phase_with_no_acs_falls_back_to_raw_tasks(self, tmp_path):
        # A phase that has plain list items (raw_tasks) but no ACs
        spec = tmp_path / "SPECS.md"
        spec.write_text(
            "## Phase 5: Tasks-Only Phase\n" "- Plain task one\n" "- Plain task two with detail\n",
            encoding="utf-8",
        )
        plan = from_spec(tmp_path, "P5")
        # 2 raw tasks → 2 steps
        assert len(plan.steps) == 2
        # Each step's description contains the task
        assert "Plain task one" in plan.steps[0].description
        assert "Plain task two" in plan.steps[1].description

    def test_empty_phase_yields_investigate_step(self, tmp_path):
        spec = tmp_path / "SPECS.md"
        spec.write_text(
            "## Phase 9: Empty\n"
            # No list items, no ACs
            "Just a description paragraph.\n",
            encoding="utf-8",
        )
        plan = from_spec(tmp_path, "P9")
        assert len(plan.steps) == 1
        assert "Investigate" in plan.steps[0].description


# ── Adapter is side-effect free ──────────────────────────────────────────


class TestAdapterIsPure:
    def test_does_not_mark_acs_done(self, workspace_with_spec):
        """A common bug: adapter triggers mark_ac_done as a side-effect.
        We assert it doesn't — mark_ac_done is the executor's job."""
        with patch("agent.core.spec_loader.mark_ac_done") as mock_mark:
            from_spec(workspace_with_spec, "P1")
            mock_mark.assert_not_called()

    def test_does_not_write_to_specs_file(self, workspace_with_spec):
        spec_path = workspace_with_spec / "SPECS.md"
        original = spec_path.read_text(encoding="utf-8")
        from_spec(workspace_with_spec, "P1")
        after = spec_path.read_text(encoding="utf-8")
        assert original == after, "Adapter must not mutate SPECS.md"


# ── Round-trip with ExecutionPlan.to_dict / from_dict ───────────────────


class TestAdapterRoundTrips:
    def test_to_dict_from_dict_preserves_all_m2_fields(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        restored = ExecutionPlan.from_dict(plan.to_dict())
        assert restored.plan_id == plan.plan_id
        assert restored.title == plan.title
        assert restored.revision == plan.revision
        assert restored.parent_plan_id == plan.parent_plan_id
        assert len(restored.steps) == len(plan.steps)
        assert len(restored.acceptance_criteria) == len(plan.acceptance_criteria)
        # TDD phase round-trips (it lives in a step field)
        for s, rs in zip(plan.steps, restored.steps):
            assert s.tdd_phase == rs.tdd_phase
            assert s.verify_command == rs.verify_command
            assert s.estimated_complexity == rs.estimated_complexity
            assert s.dependencies == rs.dependencies

    def test_to_markdown_includes_m2_sections(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        md = plan.to_markdown()
        # Acceptance criteria rendered
        assert "## Acceptance Criteria" in md
        # Allowed actions rendered
        assert "## Allowed Actions" in md
        # tdd tag appears on each step
        assert "tdd:red" in md

    def test_no_mermaid_graph_when_no_dependencies(self, workspace_with_spec):
        plan = from_spec(workspace_with_spec, "P1")
        # No dependencies set by the adapter — Mermaid graph should not appear
        md = plan.to_markdown()
        assert "```mermaid" not in md


# ── TestAC dataclass helpers ────────────────────────────────────────────


class TestSubRecordHelpers:
    """Smoke tests for the new M2 sub-records (Risk / Alternative / AC /
    AllowedPrompt) — these were added in plan.py and the adapter uses them."""

    def test_ac_to_markdown_line(self):
        from agent.core.plan import AC

        ac = AC(id="AC-P0-1", description="d", verify_command="pytest -k x")
        line = ac.to_markdown_line()
        assert "AC-P0-1" in line
        assert "d" in line
        assert "pytest -k x" in line

    def test_risk_to_markdown_line(self):
        from agent.core.plan import Risk

        r = Risk(category="data-loss", severity="high", mitigation="backup")
        line = r.to_markdown_line()
        assert "data-loss" in line
        assert "high" in line
        assert "backup" in line

    def test_alternative_to_markdown_line(self):
        from agent.core.plan import Alternative

        alt = Alternative(description="use threads", why_rejected="complex")
        line = alt.to_markdown_line()
        assert "use threads" in line
        assert "complex" in line

    def test_allowed_prompt_to_markdown_line(self):
        from agent.core.plan import AllowedPrompt

        ap = AllowedPrompt(tool="write_file", risk_level="medium", justification="files")
        line = ap.to_markdown_line()
        assert "write_file" in line
        assert "medium" in line
        assert "files" in line
