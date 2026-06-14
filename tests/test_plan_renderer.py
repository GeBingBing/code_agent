"""Tests for PlanRenderer (M3 P0).

Covers:
  * PlanRenderer Protocol conformance (duck-typed)
  * PlainTextRenderer: full / diff / collapsed / step foldable
  * RichPlanRenderer: produces Rich renderables (Panel/Tree/Table)
  * get_default_renderer factory
  * Stub backends (Editor/Web) raise NotImplementedError
"""

from __future__ import annotations

import pytest

from agent.core.plan import (
    AC,
    AllowedPrompt,
    ExecutionPlan,
    PlanStep,
    Risk,
)
from agent.core.tdd_state_machine import TDDState
from agent.ui.plan_renderer import (
    EditorPlanRenderer,
    PlainTextRenderer,
    PlanRenderer,
    RichPlanRenderer,
    WebPlanRenderer,
    get_default_renderer,
)


def _make_plan(
    *,
    steps: list[PlanStep] | None = None,
    acs: list[AC] | None = None,
    risks: list[Risk] | None = None,
    allowed_prompts: list[AllowedPrompt] | None = None,
    plan_id: str = "test-1",
    revision: int = 1,
    parent_plan_id: str = "",
    title: str = "Test Plan",
) -> ExecutionPlan:
    return ExecutionPlan(
        task="test",
        steps=steps or [PlanStep(id=1, description="step one")],
        acceptance_criteria=acs or [],
        risks=risks or [],
        allowed_prompts=allowed_prompts or [],
        plan_id=plan_id,
        revision=revision,
        parent_plan_id=parent_plan_id,
        title=title,
    )


# ── Protocol conformance ─────────────────────────────────────────────────


class TestProtocolConformance:
    def test_plain_text_satisfies_protocol(self):
        renderer: PlanRenderer = PlainTextRenderer()
        # Protocol has no __init__ in our @runtime_checkable form, so we
        # just verify the four methods exist and are callable.
        plan = _make_plan()
        assert renderer.render_full(plan) is not None
        assert renderer.render_diff(plan, plan) is not None
        assert renderer.render_collapsed(plan) is not None
        assert renderer.render_step_foldable(plan) is not None

    def test_rich_satisfies_protocol(self):
        renderer: PlanRenderer = RichPlanRenderer()
        plan = _make_plan()
        assert renderer.render_full(plan) is not None
        assert renderer.render_diff(plan, plan) is not None
        assert renderer.render_collapsed(plan) is not None
        assert renderer.render_step_foldable(plan) is not None


# ── PlainTextRenderer ────────────────────────────────────────────────────


class TestPlainTextRenderer:
    def test_render_full_uses_to_markdown(self):
        plan = _make_plan(title="My Plan")
        result = PlainTextRenderer().render_full(plan)
        text = str(result)
        assert "## Plan: My Plan" in text
        assert "step one" in text

    def test_render_diff_no_changes(self):
        plan = _make_plan()
        result = PlainTextRenderer().render_diff(plan, plan)
        text = str(result)
        assert "no step changes" in text

    def test_render_diff_added_step(self):
        old = _make_plan(steps=[PlanStep(id=1, description="a")])
        new = _make_plan(steps=[PlanStep(id=1, description="a"), PlanStep(id=2, description="b")])
        text = str(PlainTextRenderer().render_diff(old, new))
        assert "+ Step 2: b" in text

    def test_render_diff_removed_step(self):
        old = _make_plan(steps=[PlanStep(id=1, description="a"), PlanStep(id=2, description="b")])
        new = _make_plan(steps=[PlanStep(id=1, description="a")])
        text = str(PlainTextRenderer().render_diff(old, new))
        assert "- Step 2: b" in text

    def test_render_diff_modified_step(self):
        old = _make_plan(steps=[PlanStep(id=1, description="old desc")])
        new = _make_plan(steps=[PlanStep(id=1, description="new desc")])
        text = str(PlainTextRenderer().render_diff(old, new))
        assert "Step 1" in text
        assert "old desc" in text
        assert "new desc" in text

    def test_render_diff_status_change(self):
        old = _make_plan(steps=[PlanStep(id=1, description="x", status="pending")])
        new = _make_plan(steps=[PlanStep(id=1, description="x", status="done")])
        text = str(PlainTextRenderer().render_diff(old, new))
        assert "status" in text
        assert "pending" in text
        assert "done" in text

    def test_render_diff_ac_count_delta(self):
        old = _make_plan(acs=[AC(id="AC-1", description="d")])
        new = _make_plan(acs=[AC(id="AC-1", description="d"), AC(id="AC-2", description="d2")])
        text = str(PlainTextRenderer().render_diff(old, new))
        assert "AC count" in text

    def test_render_collapsed_includes_revision_and_count(self):
        plan = _make_plan(
            revision=3,
            parent_plan_id="parent-1",
            steps=[
                PlanStep(id=1, description="a", status="done"),
                PlanStep(id=2, description="b", status="pending"),
            ],
            acs=[AC(id="AC-1", description="d")],
        )
        text = str(PlainTextRenderer().render_collapsed(plan))
        assert "1 done" in text or "1/2" in text or "pending" in text
        assert "revision 3" in text
        assert "AC" in text

    def test_render_step_foldable_returns_per_step_list(self):
        plan = _make_plan(
            steps=[
                PlanStep(id=1, description="alpha"),
                PlanStep(id=2, description="beta"),
                PlanStep(id=3, description="gamma"),
            ]
        )
        result = PlainTextRenderer().render_step_foldable(plan)
        assert len(result) == 3
        assert "alpha" in str(result[0])
        assert "beta" in str(result[1])
        assert "gamma" in str(result[2])


# ── RichPlanRenderer ─────────────────────────────────────────────────────


class TestRichPlanRenderer:
    def test_render_full_returns_rich_panel(self):
        plan = _make_plan(title="Rich Plan")
        result = RichPlanRenderer().render_full(plan)
        # Rich Panel has a 'title' attribute
        assert hasattr(result, "title")
        # The body is a Tree (M3 design)
        from rich.tree import Tree

        assert isinstance(result.renderable, Tree)

    def test_render_collapsed_returns_rich_panel(self):
        plan = _make_plan()
        result = RichPlanRenderer().render_collapsed(plan)
        from rich.table import Table

        assert hasattr(result, "title")
        assert isinstance(result.renderable, Table)

    def test_render_step_foldable_returns_per_step_panels(self):
        plan = _make_plan(
            steps=[
                PlanStep(id=1, description="alpha", verify_command="pytest -k a"),
                PlanStep(id=2, description="beta", tdd_phase=TDDState.RED),
            ]
        )
        result = RichPlanRenderer().render_step_foldable(plan)
        assert len(result) == 2
        # Each entry is a Panel
        for r in result:
            assert hasattr(r, "title")
            assert hasattr(r, "renderable")

    def test_render_full_includes_dependencies_and_verify(self):
        plan = _make_plan(
            steps=[
                PlanStep(
                    id=1,
                    description="first",
                    verify_command="pytest -k first",
                    dependencies=[2],  # forward-ref, just for rendering
                ),
            ]
        )
        result = RichPlanRenderer().render_full(plan)
        # The Tree renderable should be retrievable as text
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        Console(file=buf, width=200, force_terminal=False).print(result)
        text = buf.getvalue()
        assert "first" in text
        assert "deps" in text
        assert "verify" in text

    def test_render_diff_panels(self):
        old = _make_plan(steps=[PlanStep(id=1, description="a")])
        new = _make_plan(steps=[PlanStep(id=1, description="b")])
        result = RichPlanRenderer().render_diff(old, new)
        assert hasattr(result, "title")
        # Body should reflect the change
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        Console(file=buf, width=200, force_terminal=False).print(result)
        text = buf.getvalue()
        assert "Step 1" in text
        assert "r1" in text  # old revision
        assert "r1" in text  # new revision

    def test_status_icons(self):
        renderer = RichPlanRenderer()
        for status, icon in [
            ("pending", "○"),
            ("in_progress", "◉"),
            ("done", "✓"),
            ("skipped", "−"),
            ("failed", "✗"),
        ]:
            assert renderer._icon(status) == icon


# ── get_default_renderer factory ─────────────────────────────────────────


class TestGetDefaultRenderer:
    def test_returns_rich_when_available(self):
        # rich is in our dev deps
        r = get_default_renderer()
        assert isinstance(r, RichPlanRenderer)

    def test_factory_returns_protocol_compliant(self):
        r = get_default_renderer()
        assert isinstance(r, PlanRenderer)


# ── Stub backends raise NotImplementedError ─────────────────────────────


class TestStubBackends:
    def test_editor_renderer_raises(self):
        r = EditorPlanRenderer()
        plan = _make_plan()
        with pytest.raises(NotImplementedError):
            r.render_full(plan)

    def test_web_renderer_raises(self):
        r = WebPlanRenderer()
        plan = _make_plan()
        with pytest.raises(NotImplementedError):
            r.render_full(plan)

    def test_editor_renderer_also_raises_for_diff(self):
        plan = _make_plan()
        with pytest.raises(NotImplementedError):
            EditorPlanRenderer().render_diff(plan, plan)


# ── M2 plan data round-trips through renderer ──────────────────────────


class TestRendererWithM2Plan:
    def test_full_renders_m2_fields(self):
        plan = _make_plan(
            steps=[
                PlanStep(
                    id=1,
                    description="implement X",
                    tdd_phase=TDDState.RED,
                    estimated_complexity="M",
                ),
            ],
            acs=[AC(id="AC-P0-1", description="X works", verify_command="pytest -k x")],
            risks=[Risk(category="data-loss", severity="medium", mitigation="backup")],
            allowed_prompts=[AllowedPrompt(tool="edit", risk_level="low", justification="files")],
        )
        result = RichPlanRenderer().render_full(plan)
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        Console(file=buf, width=200, force_terminal=False).print(result)
        text = buf.getvalue()
        # All M2 sections should be present
        assert "Steps" in text
        assert "Acceptance Criteria" in text
        assert "Risks" in text
        assert "Allowed Actions" in text
        # TDD + size tags
        assert "tdd:red" in text
        assert "size:M" in text
