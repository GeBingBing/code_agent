"""Tests for the plan_mode tool surface (M1 P0).

These pin down the M1 contract:
  * EnterPlanModeTool returns a structured ``mode=plan`` response with the
    whitelist embedded in metadata (so the LLM can self-describe what it
    can do next).
  * ExitPlanModeTool validates the plan (non-empty, contains checklist),
    writes the plan to ``~/.coding-agent/plans/<id>.md`` with frontmatter,
    and returns ``plan_id`` / ``persistence_path`` in metadata.
  * ExitPlanModeTool rejects empty plans and plans without checklist items
    (with a helpful error).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.tools.base import ToolResult
from agent.tools.plan_mode import (
    EnterPlanModeTool,
    ExitPlanModeTool,
    _new_plan_id,
    _slugify,
    _validate_plan,
)

# ── EnterPlanModeTool ──────────────────────────────────────────────────────


class TestEnterPlanModeTool:
    def test_returns_success(self):
        tool = EnterPlanModeTool()
        result = asyncio.run(tool.execute())
        assert isinstance(result, ToolResult)
        assert result.success is True

    def test_response_mentions_plan_mode(self):
        tool = EnterPlanModeTool()
        result = asyncio.run(tool.execute())
        assert "Plan mode activated" in result.content

    def test_metadata_signals_new_mode(self):
        tool = EnterPlanModeTool()
        result = asyncio.run(tool.execute())
        assert result.metadata is not None
        assert result.metadata["mode"] == "plan"

    def test_metadata_lists_tools_whitelist(self):
        tool = EnterPlanModeTool()
        result = asyncio.run(tool.execute())
        whitelist = result.metadata["tools_whitelist"]
        # All whitelist tools must actually be in the response
        assert "read_file" in whitelist
        assert "grep" in whitelist
        assert "code_search" in whitelist
        assert "web_fetch" in whitelist
        assert "web_search" in whitelist
        # Plan transitions are listed so the LLM knows it can exit
        assert "enter_plan_mode" in whitelist
        assert "exit_plan_mode" in whitelist
        # Write tools MUST NOT be in the whitelist
        assert "write_file" not in whitelist
        assert "execute_command" not in whitelist
        assert "apply_diff" not in whitelist

    def test_metadata_records_transition_source(self):
        tool = EnterPlanModeTool()
        result = asyncio.run(tool.execute())
        assert result.metadata["transitioned_by"] == "tool"


# ── ExitPlanModeTool — validation ───────────────────────────────────────────


class TestExitPlanModeValidation:
    def test_empty_plan_rejected(self):
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan=""))
        assert result.success is False
        assert "empty" in (result.error or "").lower()
        assert result.metadata == {"validation": "rejected"}

    def test_whitespace_only_plan_rejected(self):
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="   \n\n  "))
        assert result.success is False
        assert "empty" in (result.error or "").lower()

    def test_plan_without_checklist_rejected(self):
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="Just write some code, no structure."))
        assert result.success is False
        assert "checklist" in (result.error or "").lower()

    def test_plan_with_only_done_checklist_accepted(self):
        """Regression: - [x] (already-done) should also count as a valid
        checklist item — agents sometimes pre-tick steps they consider
        trivial."""
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="- [x] Already done\n- [x] Another done"))
        assert result.success is True

    def test_valid_plan_with_steps_accepted(self):
        tool = ExitPlanModeTool()
        result = asyncio.run(
            tool.execute(plan="## Plan\n- [ ] Step 1: read file\n- [ ] Step 2: write fix")
        )
        assert result.success is True


# ── ExitPlanModeTool — persistence ──────────────────────────────────────────


class TestExitPlanModePersistence:
    def test_writes_file_to_plans_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        tool = ExitPlanModeTool()
        asyncio.run(tool.execute(plan="- [ ] Read code"))
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].suffix == ".md"

    def test_metadata_has_plan_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="- [ ] Read code"))
        assert "plan_id" in result.metadata
        assert result.metadata["plan_id"].startswith("plan-")

    def test_metadata_has_persistence_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="- [ ] Read code"))
        path = Path(result.metadata["persistence_path"])
        assert path.parent == tmp_path
        assert path.exists()

    def test_metadata_counts_steps(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        tool = ExitPlanModeTool()
        result = asyncio.run(
            tool.execute(plan="- [ ] One\n- [ ] Two\n- [ ] Three\n- [x] Four (done)")
        )
        assert result.metadata["step_count"] == 4

    def test_metadata_includes_allowed_prompts(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="- [ ] x", allowed_prompts="edit files, run tests"))
        assert "edit files" in result.metadata["allowed_prompts"]
        assert "run tests" in result.metadata["allowed_prompts"]

    def test_persistence_path_uses_plan_id(self, tmp_path, monkeypatch):
        """Filename must equal plan_id (with filesystem-safe substitution)
        so users can find the file by plan_id alone."""
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="- [ ] x"))
        plan_id = result.metadata["plan_id"]
        expected = tmp_path / f"{plan_id}.md"
        assert Path(result.metadata["persistence_path"]) == expected
        assert expected.exists()

    def test_persisted_file_has_frontmatter(self, tmp_path, monkeypatch):
        """M1 P0 contract: persisted plans start with a `plan_id` heading
        so ``/plan show <id>`` and ``coding-agent --resume <id>`` can
        locate them."""
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="- [ ] x", allowed_prompts="run tests"))
        content = Path(result.metadata["persistence_path"]).read_text()
        # Plan-id heading
        assert content.startswith(f"# {result.metadata['plan_id']}")
        # Allowed prompts captured
        assert "run tests" in content
        # Original plan body preserved
        assert "- [ ] x" in content


# ── Engine injection (forward-compat) ───────────────────────────────────────


class TestExitPlanModeEngineInjection:
    """M1 P0 forward-compat: when an engine with set_current_plan is
    injected, ExitPlanModeTool should record the plan_id. When no engine
    is injected (the common case — the dispatcher doesn't pass one yet),
    the tool must still succeed and still return the structured metadata.
    """

    def test_records_plan_id_when_engine_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)

        class _FakeEngine:
            def __init__(self):
                self.recorded = None

            def set_current_plan(self, plan_id: str) -> None:
                self.recorded = plan_id

        engine = _FakeEngine()
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="- [ ] x", engine=engine))
        assert result.success is True
        assert engine.recorded == result.metadata["plan_id"]

    def test_succeeds_without_engine(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)
        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="- [ ] x"))
        assert result.success is True
        # No engine, no error — structured metadata still complete.
        assert "plan_id" in result.metadata

    def test_engine_failure_does_not_break_tool(self, tmp_path, monkeypatch):
        """If the engine's set_current_plan raises, the tool must still
        return a successful ToolResult. The plan is on disk; the LLM and
        user can recover via the metadata path."""
        monkeypatch.setattr("agent.tools.plan_mode._plan_dir", lambda: tmp_path)

        class _BrokenEngine:
            def set_current_plan(self, plan_id: str) -> None:
                raise RuntimeError("simulated engine failure")

        tool = ExitPlanModeTool()
        result = asyncio.run(tool.execute(plan="- [ ] x", engine=_BrokenEngine()))
        assert result.success is True


# ── Helpers ─────────────────────────────────────────────────────────────────


class TestSlugify:
    def test_basic(self):
        assert _slugify("Hello World") == "hello-world"

    def test_strips_special_chars(self):
        assert _slugify("Fix /path/to/file.py") == "fix-path-to-file-py"

    def test_handles_empty(self):
        assert _slugify("") == "plan"

    def test_truncates_long_input(self):
        s = "a" * 100
        assert len(_slugify(s)) == 40


class TestNewPlanId:
    def test_format(self):
        pid = _new_plan_id("Fix bug in dispatcher")
        assert pid.startswith("plan-fix-bug-in-dispatcher-")
        # Contains timestamp + uuid suffix
        parts = pid.split("-")
        assert len(parts) >= 3

    def test_unique_per_call(self):
        ids = {_new_plan_id("x") for _ in range(5)}
        # Timestamps may collide for very fast loops; uuid suffix guarantees
        # 5-digit hex uniqueness, so we expect 5 distinct ids.
        assert len(ids) == 5


class TestValidatePlan:
    def test_empty(self):
        assert _validate_plan("") is not None

    def test_no_checklist(self):
        assert _validate_plan("Just a paragraph with no list.") is not None

    def test_valid(self):
        assert _validate_plan("- [ ] step 1") is None

    def test_unicode_task(self):
        """Regression: slugify must not crash on non-ASCII task text."""
        assert _validate_plan("- [ ] 修复 dispatcher 中的 bug") is None
        pid = _new_plan_id("修复 dispatcher 中的 bug")
        assert "plan-" in pid
