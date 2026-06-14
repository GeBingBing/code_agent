"""Tests for /plan refine and /plan comment (M3 P0).

Covers:
  * /plan refine bumps revision, chains parent_plan_id, archives parent
  * Refine with the no-op refiner shows "no step changes" diff
  * Refine with a custom refiner actually changes the plan
  * Refine archives the parent to ~/.coding-agent/plans/<id>-r<n>.md
  * Refine re-attaches the static review
  * /plan comment appends a comment to a step
  * Edge cases: no plan, bad step number, etc.
"""

from __future__ import annotations

import asyncio

from agent.commands.builtin import _handle_plan
from agent.core.plan import ExecutionPlan, PlanStep


class _FakeCli:
    def __init__(self, plan=None):
        self._last_plan = plan
        self._last_plan_persistence_path = None


def _ctx(cli: _FakeCli | None = None, plan_refiner=None) -> dict:
    return {"cli": cli, "engine": None, "workspace": ".", "plan_refiner": plan_refiner}


def _make_plan(
    *, n: int = 2, plan_id: str = "spec-P0-1700000000-abc", revision: int = 1
) -> ExecutionPlan:
    return ExecutionPlan(
        task="test",
        steps=[PlanStep(id=i + 1, description=f"step {i+1}") for i in range(n)],
        plan_id=plan_id,
        revision=revision,
        title="Test",
    )


# ── /plan refine — happy paths ────────────────────────────────────────────


class TestRefineHappyPath:
    def test_refine_bumps_revision(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        plan = _make_plan(plan_id="spec-P0-100-abc", revision=1)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("refine", _ctx(cli)))
        # revision bumped
        assert cli._last_plan.revision == 2
        # Result mentions parent + new revision
        assert "r2" in result
        assert "spec-P0-100-abc" in result or "parent" in result.lower()

    def test_refine_chains_parent_plan_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        plan = _make_plan(plan_id="spec-P0-200-xyz", revision=1)
        cli = _FakeCli(plan)
        asyncio.run(_handle_plan("refine", _ctx(cli)))
        # The NEW plan's parent_plan_id should be the OLD plan's plan_id
        assert cli._last_plan.parent_plan_id == "spec-P0-200-xyz"

    def test_refine_archives_parent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        plan = _make_plan(plan_id="spec-P0-300-qqq", revision=1)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("refine", _ctx(cli)))
        # A history file was written
        history_files = list(tmp_path.glob("*.md"))
        assert len(history_files) == 1
        # The filename includes the plan_id + revision
        assert "spec-P0-300-qqq" in history_files[0].name
        assert "r1" in history_files[0].name
        # The result mentions the archive
        assert "Archived" in result

    def test_refine_noop_refiner_shows_no_step_changes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        plan = _make_plan(plan_id="spec-P0-400", revision=1)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("refine", _ctx(cli)))
        # Default refiner returns the same plan, so diff should say
        # "no step changes"
        assert "no step changes" in result

    def test_refine_attaches_review_notes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        plan = _make_plan(plan_id="spec-P0-500", revision=1)
        cli = _FakeCli(plan)
        asyncio.run(_handle_plan("refine", _ctx(cli)))
        # M2 review_notes is set on the refined plan
        assert "## Review" in cli._last_plan.review_notes

    def test_refine_with_custom_refiner_applies_changes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        plan = _make_plan(plan_id="spec-P0-600", revision=1)
        cli = _FakeCli(plan)

        # Custom refiner: adds a 3rd step focused on the user's hint
        def refiner(p, focus):
            return (
                f"## Refined Plan\n\n"
                f"- [ ] Step 1: rewritten first step\n"
                f"- [ ] Step 2: rewritten second step\n"
                f"- [ ] Step 3: NEW step for focus={focus or 'none'}\n"
            )

        result = asyncio.run(_handle_plan("refine focusX", _ctx(cli, plan_refiner=refiner)))
        # 3 steps now (was 2)
        assert len(cli._last_plan.steps) == 3
        # Diff visible in output
        assert "Step 3" in result
        # The new step description mentions the focus
        assert "focusX" in cli._last_plan.steps[2].description

    def test_refine_refiner_failure_keeps_parent_archived(self, tmp_path, monkeypatch):
        """If the refiner raises, the parent is still archived but the
        refine is rejected (parent stays on cli._last_plan so the user
        can retry)."""
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        plan = _make_plan(plan_id="spec-P0-700", revision=1)
        cli = _FakeCli(plan)

        def bad_refiner(p, focus):
            raise RuntimeError("LLM offline")

        result = asyncio.run(_handle_plan("refine", _ctx(cli, plan_refiner=bad_refiner)))
        assert "Refiner failed" in result
        # Parent is archived but cli._last_plan is unchanged
        assert cli._last_plan.plan_id == "spec-P0-700"
        assert cli._last_plan.revision == 1
        # History file written
        assert len(list(tmp_path.glob("*.md"))) == 1


# ── /plan refine — error paths ────────────────────────────────────────────


class TestRefineErrors:
    def test_refine_without_plan_returns_friendly_message(self):
        cli = _FakeCli(plan=None)
        result = asyncio.run(_handle_plan("refine", _ctx(cli)))
        assert "No plan" in result

    def test_refine_without_cli_returns_friendly(self):
        """No cli in ctx — refine returns a friendly dimmed message
        (we can't archive or stash without cli, so refuse cleanly)."""
        result = asyncio.run(_handle_plan("refine", _ctx(cli=None)))
        assert "No plan" in result


# ── /plan comment ─────────────────────────────────────────────────────────


class TestComment:
    def test_comment_attaches_text_to_step(self):
        plan = _make_plan(n=3)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("comment 2 add error handling here", _ctx(cli)))
        assert "Step 2" in result
        # The step has a comments list
        assert hasattr(plan.steps[1], "comments")
        assert len(plan.steps[1].comments) == 1
        assert plan.steps[1].comments[0]["text"] == "add error handling here"

    def test_comment_preserves_existing(self):
        plan = _make_plan(n=2)
        plan.steps[0].comments = [{"text": "first", "by": "user", "at": "now"}]
        cli = _FakeCli(plan)
        asyncio.run(_handle_plan("comment 1 second note", _ctx(cli)))
        assert len(plan.steps[0].comments) == 2
        assert plan.steps[0].comments[1]["text"] == "second note"

    def test_comment_handles_long_text(self):
        plan = _make_plan(n=1)
        cli = _FakeCli(plan)
        long_text = "this is a very long comment that contains many words and should be stored verbatim without truncation"
        asyncio.run(_handle_plan(f"comment 1 {long_text}", _ctx(cli)))
        assert plan.steps[0].comments[0]["text"] == long_text

    def test_comment_includes_by_and_at_metadata(self):
        plan = _make_plan(n=1)
        cli = _FakeCli(plan)
        asyncio.run(_handle_plan("comment 1 x", _ctx(cli)))
        comment = plan.steps[0].comments[0]
        assert "by" in comment
        assert "at" in comment


# ── /plan comment — error paths ───────────────────────────────────────────


class TestCommentErrors:
    def test_no_plan_returns_friendly(self):
        cli = _FakeCli(plan=None)
        result = asyncio.run(_handle_plan("comment 1 x", _ctx(cli)))
        assert "No plan" in result

    def test_no_args_shows_usage(self):
        plan = _make_plan()
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("comment", _ctx(cli)))
        assert "Usage" in result

    def test_non_numeric_step_rejected(self):
        plan = _make_plan()
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("comment abc hello", _ctx(cli)))
        assert "step number" in result.lower()

    def test_out_of_range_step_rejected(self):
        plan = _make_plan(n=2)
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("comment 99 x", _ctx(cli)))
        assert "does not exist" in result.lower()

    def test_missing_text_shows_usage(self):
        plan = _make_plan()
        cli = _FakeCli(plan)
        result = asyncio.run(_handle_plan("comment 1", _ctx(cli)))
        assert "Usage" in result


# ── Integration: refine chains with from-spec ────────────────────────────


class TestRefineAfterFromSpec:
    def test_refine_works_on_from_spec_output(self, tmp_path, monkeypatch):
        """End-to-end: /plan from-spec → /plan refine."""
        from agent.core.spec_plan_adapter import from_spec as spec_from_spec

        spec = tmp_path / "SPECS.md"
        spec.write_text("## Phase 0: Setup\n- [ ] init\n- [ ] install\n", encoding="utf-8")
        # Need plan_dir for refine's archive
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)

        plan = spec_from_spec(tmp_path, "P0")
        cli = _FakeCli(plan)
        # Refine (no-op refiner since no LLM)
        result = asyncio.run(_handle_plan("refine", _ctx(cli)))
        # The refined plan has revision=2 and parent_plan_id set
        assert cli._last_plan.revision == 2
        assert cli._last_plan.parent_plan_id == plan.plan_id
        # 2 steps still
        assert len(cli._last_plan.steps) == 2
