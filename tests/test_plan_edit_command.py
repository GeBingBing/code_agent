"""Tests for the /plan edit command (M1 P0: was a no-op stub).

The original /plan edit (builtin.py:83-90) returned a hard-coded
"Step N updated to: ..." string and never touched the actual
ExecutionPlan object. These tests pin down the fixed behaviour:
  * edit <N> <description>          (legacy free-form)
  * edit <N> <field> <value>        (structured, where field ∈
                                    {description, tool_hint, expected_outcome})
  * Unknown field names are rejected (no silent attribute creation)
  * Out-of-range step numbers are rejected
  * No-plan case returns a friendly dimmed message instead of crashing
  * When a persistence_path is tracked, the on-disk file is rewritten
"""

from __future__ import annotations

import asyncio

from agent.commands.builtin import _handle_plan
from agent.core.plan import ExecutionPlan, PlanStep


def _make_plan_with(n: int = 3) -> ExecutionPlan:
    return ExecutionPlan(
        task="Test task",
        steps=[
            PlanStep(
                id=i + 1,
                description=f"step {i+1} description",
                tool_hint=f"tool_{i+1}",
                expected_outcome=f"outcome {i+1}",
            )
            for i in range(n)
        ],
    )


class _FakeCli:
    def __init__(self, plan=None, persistence_path=None):
        self._last_plan = plan
        self._last_plan_persistence_path = persistence_path


def _ctx(cli: _FakeCli | None) -> dict:
    return {"cli": cli, "engine": None}


# ── Legacy free-form (preserves original syntax) ────────────────────────────


class TestLegacyEditForm:
    def test_edit_step_2_description(self):
        plan = _make_plan_with(3)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("edit 2 step two rewritten", _ctx(cli)))
        assert "Step 2.description updated" in result
        assert plan.steps[1].description == "step two rewritten"
        # Other fields untouched
        assert plan.steps[1].tool_hint == "tool_2"
        assert plan.steps[1].expected_outcome == "outcome 2"

    def test_edit_step_1_preserves_legacy_message_shape(self):
        """The legacy /plan edit had a green-check + "Step N updated to: ..."
        message. The new structured form replaces "to: " with ".description
        updated to: " — same shape, more honest about the field."""
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("edit 1 new description", _ctx(cli)))
        assert "Step 1.description updated to: new description" in result

    def test_edit_preserves_other_steps(self):
        plan = _make_plan_with(3)
        cli = _FakeCli(plan)
        asyncio.run(_handle_plan("edit 1 changed", _ctx(cli)))
        # Steps 2 and 3 should be byte-for-byte identical
        assert plan.steps[1].description == "step 2 description"
        assert plan.steps[2].description == "step 3 description"


# ── Structured form ─────────────────────────────────────────────────────────


class TestStructuredEditForm:
    def test_edit_tool_hint(self):
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("edit 1 tool_hint read_file", _ctx(cli)))
        assert "Step 1.tool_hint updated" in result
        assert plan.steps[0].tool_hint == "read_file"

    def test_edit_expected_outcome(self):
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("edit 2 expected_outcome all green", _ctx(cli)))
        assert "Step 2.expected_outcome updated" in result
        assert plan.steps[1].expected_outcome == "all green"

    def test_field_name_is_case_insensitive(self):
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        # Mixed case field name
        asyncio.run(_handle_plan("edit 1 TOOL_HINT grep", _ctx(cli)))
        assert plan.steps[0].tool_hint == "grep"

    def test_field_value_can_contain_spaces(self):
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        asyncio.run(_handle_plan("edit 1 description two three four", _ctx(cli)))
        assert plan.steps[0].description == "two three four"


# ── Validation / error paths ────────────────────────────────────────────────


class TestEditRejects:
    def test_unknown_field_falls_back_to_legacy(self):
        """Design decision: when the second token is not a known field
        name, we treat the whole tail as the description (legacy
        free-form). This is a forgiving fallback for users who learnt
        the original command in earlier versions — `edit 1 write the
        read_file step` still works.

        The M2 EditOp schema will be stricter; for M1 the
        "fail-safe to legacy" behaviour wins.
        """
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("edit 1 bogus_field value", _ctx(cli)))
        # Falls back to legacy — the entire tail becomes the description
        assert "Step 1.description updated" in result
        assert plan.steps[0].description == "bogus_field value"
        # Plan must NOT have been mutated with the bogus field
        assert not hasattr(plan.steps[0], "bogus_field")

    def test_step_out_of_range_rejected(self):
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("edit 5 anything", _ctx(cli)))
        assert "does not exist" in result.lower()

    def test_zero_step_rejected(self):
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("edit 0 anything", _ctx(cli)))
        assert "does not exist" in result.lower()

    def test_no_plan_returns_friendly_message(self):
        cli = _FakeCli(plan=None)
        result = asyncio.run(_handle_plan("edit 1 anything", _ctx(cli)))
        # dimmed message — no crash
        assert "no plan" in result.lower()

    def test_non_numeric_step_rejected(self):
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("edit abc anything", _ctx(cli)))
        assert "step number" in result.lower()

    def test_missing_args_shows_usage(self):
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("edit", _ctx(cli)))
        assert "usage" in result.lower()


# ── Disk persistence ────────────────────────────────────────────────────────


class TestEditPersistsToDisk:
    def test_rewrites_file_when_path_known(self, tmp_path):
        plan_file = tmp_path / "plan-test.md"
        plan_file.write_text(
            "# plan-test\n**Allowed prompts:** All actions\n\n---\n\n"
            + _make_plan_with(2).to_markdown()
            + "\n",
            encoding="utf-8",
        )
        plan = _make_plan_with(2)
        cli = _FakeCli(plan, persistence_path=str(plan_file))
        result = asyncio.run(_handle_plan("edit 1 new desc on disk", _ctx(cli)))
        # Persisted message
        assert "Persisted to" in result
        # File rewritten
        on_disk = plan_file.read_text(encoding="utf-8")
        assert "new desc on disk" in on_disk
        # Plan object also updated
        assert plan.steps[0].description == "new desc on disk"

    def test_no_persistence_path_silently_succeeds(self, tmp_path):
        """If we have no path tracked (legacy plans), just update the
        in-memory plan — don't pretend to persist."""
        plan = _make_plan_with(2)
        cli = _FakeCli(plan, persistence_path=None)
        result = asyncio.run(_handle_plan("edit 1 new desc", _ctx(cli)))
        assert "updated" in result.lower()
        assert "persisted" not in result.lower()
        assert plan.steps[0].description == "new desc"

    def test_missing_persistence_file_does_not_crash(self, tmp_path):
        plan = _make_plan_with(2)
        cli = _FakeCli(plan, persistence_path=str(tmp_path / "nonexistent.md"))
        result = asyncio.run(_handle_plan("edit 1 new desc", _ctx(cli)))
        # Should still update in-memory; persistence falls through silently
        assert "updated" in result.lower()
        assert plan.steps[0].description == "new desc"


# ── Show / accept / reject paths still work ─────────────────────────────────


class TestOtherPlanCommands:
    def test_show_renders_last_plan(self):
        plan = _make_plan_with(2)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("show", _ctx(cli)))
        assert "step 1 description" in result

    def test_show_no_plan(self):
        cli = _FakeCli(plan=None)
        result = asyncio.run(_handle_plan("show", _ctx(cli)))
        assert "no plan" in result.lower()

    def test_accept_switches_mode(self, monkeypatch):
        """The accept path mutates engine.permissions.mode and AGENT_MODE.
        It should still do that — we don't change the M1 behaviour here.
        We just verify the function returns the green-check message."""
        # Provide a minimal engine stub
        from agent.core.permissions import PermissionMode

        class _FakePerm:
            mode = PermissionMode("plan")

        class _FakeEngine:
            permissions = _FakePerm()

        ctx = {"cli": _FakeCli(None), "engine": _FakeEngine()}
        result = asyncio.run(_handle_plan("accept", ctx))
        assert "accepted" in result.lower()
        # The engine permission should have been flipped to default
        assert ctx["engine"].permissions.mode == PermissionMode("default")

    def test_reject_returns_dimmed_message(self):
        result = asyncio.run(_handle_plan("reject", _ctx(_FakeCli(None))))
        assert "discarded" in result.lower() or "re-plan" in result.lower()
