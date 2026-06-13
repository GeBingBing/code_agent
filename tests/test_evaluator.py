"""Tests for the Evaluator Agent (PR-09)."""

import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.agents.evaluator import (
    DIMENSIONS,
    EvaluationReport,
    EvaluationScore,
    EvaluatorAgent,
    _parse_score_response,
)

# ── Dataclasses ──────────────────────────────────────────────────


class TestEvaluationScore:
    def test_basic(self):
        s = EvaluationScore(dimension="completion", score=8.5, rationale="ok")
        assert s.dimension == "completion"
        assert s.score == 8.5
        assert s.rationale == "ok"

    def test_clamps_high(self):
        s = EvaluationScore(dimension="x", score=15)
        assert s.score == 10.0

    def test_clamps_low(self):
        s = EvaluationScore(dimension="x", score=-3)
        assert s.score == 0.0

    def test_handles_bad_type(self):
        s = EvaluationScore(dimension="x", score="not a number")
        assert s.score == 0.0


class TestEvaluationReport:
    def test_overall_score_auto_computed(self):
        r = EvaluationReport(
            task="t",
            agent_id="main",
            scores=[
                EvaluationScore("a", 8),
                EvaluationScore("b", 6),
                EvaluationScore("c", 10),
                EvaluationScore("d", 4),
            ],
        )
        assert r.overall_score == 7.0

    def test_overall_score_explicit_preserved(self):
        r = EvaluationReport(
            task="t",
            agent_id="main",
            scores=[EvaluationScore("a", 8)],
            overall_score=5.5,
        )
        assert r.overall_score == 5.5

    def test_evaluated_at_auto_set(self):
        r = EvaluationReport(task="t", agent_id="main", scores=[])
        assert re.match(r"\d{4}-\d{2}-\d{2}T", r.evaluated_at)
        assert r.evaluated_at.endswith("Z")

    def test_to_dict_roundtrip(self):
        r = EvaluationReport(
            task="t",
            agent_id="main",
            scores=[EvaluationScore("completion", 9, "ok")],
            findings=["finding 1"],
            suggestions=["suggest 1"],
        )
        d = r.to_dict()
        assert d["task"] == "t"
        assert d["scores"][0]["dimension"] == "completion"
        assert d["findings"] == ["finding 1"]
        assert d["suggestions"] == ["suggest 1"]

    def test_to_json_parses(self):
        r = EvaluationReport(
            task="t",
            agent_id="main",
            scores=[EvaluationScore("completion", 9)],
        )
        j = r.to_json()
        parsed = json.loads(j)
        assert parsed["task"] == "t"

    def test_to_markdown_structure(self):
        r = EvaluationReport(
            task="impl X",
            agent_id="main",
            scores=[
                EvaluationScore("completion", 9, "all AC met"),
                EvaluationScore("code_quality", 7, "clean code"),
            ],
            findings=["✅ tests pass"],
            suggestions=["Add docstrings"],
        )
        md = r.to_markdown()
        assert "# Task Evaluation" in md
        assert "impl X" in md
        assert "**completion**: 9.0/10" in md
        assert "**总分**" in md
        assert "## Findings" in md
        assert "✅ tests pass" in md
        assert "## 建议改进" in md
        assert "Add docstrings" in md

    def test_to_markdown_omits_empty_sections(self):
        r = EvaluationReport(
            task="t",
            agent_id="main",
            scores=[EvaluationScore("completion", 9)],
        )
        md = r.to_markdown()
        assert "## Findings" not in md
        assert "## 建议改进" not in md


# ── EvaluatorAgent ───────────────────────────────────────────────


class TestModelSelection:
    def _make_engine(self, model: str):
        e = MagicMock()
        e.config.model = model
        e.llm = None
        return e

    def test_picks_claude_for_gpt(self):
        ev = EvaluatorAgent(self._make_engine("gpt-4o"))
        assert "claude" in ev.model.lower()

    def test_picks_gpt_for_claude(self):
        ev = EvaluatorAgent(self._make_engine("claude-sonnet-4-6"))
        assert "gpt" in ev.model.lower()

    def test_explicit_model_wins(self):
        ev = EvaluatorAgent(self._make_engine("gpt-4"), model="MiniMax-M3")
        assert ev.model == "MiniMax-M3"

    def test_unknown_model_falls_back(self):
        ev = EvaluatorAgent(self._make_engine("ollama:llama3"))
        # Falls back to main model — better same-family judge than none
        assert ev.model == "ollama:llama3"


class TestEvidenceCollection:
    def setup_method(self):
        eng = MagicMock()
        eng.config.model = "gpt-4o"
        eng.llm = None
        self.ev = EvaluatorAgent(eng)

    def test_basic_counts(self):
        audit = [
            {"action": "tool_call", "tool": "read_file"},
            {"action": "tool_call", "tool": "write_file"},
            {"action": "tool_result", "tool": "read_file"},
        ]
        e = self.ev._gather_evidence(task="t", agent_id="main", audit_records=audit)
        assert e["tool_calls"] == 2
        assert e["tool_results"] == 1
        assert e["tools_used"] == {"read_file": 2, "write_file": 1}

    def test_errors_extracted(self):
        audit = [
            {"action": "tool_result", "tool": "x", "error": "boom"},
            {"action": "tool_result", "tool": "y", "error": None},
        ]
        e = self.ev._gather_evidence(task="t", agent_id="main", audit_records=audit)
        assert len(e["errors"]) == 1

    def test_permission_decisions_counted(self):
        audit = [
            {"permission_decision": "allow"},
            {"permission_decision": "allow"},
            {"permission_decision": "deny"},
        ]
        e = self.ev._gather_evidence(task="t", agent_id="main", audit_records=audit)
        assert e["permission_decisions"]["allow"] == 2
        assert e["permission_decisions"]["deny"] == 1

    def test_test_runs_summarised(self):
        audit = [
            {"tool": "run_tests", "duration_ms": 100},
            {"tool": "run_tests", "duration_ms": 200, "error": "1 failed"},
        ]
        e = self.ev._gather_evidence(task="t", agent_id="main", audit_records=audit)
        assert e["test_runs"] == 2
        assert e["last_test_outcome"]["error"] == "1 failed"

    def test_no_workspace_no_git(self):
        e = self.ev._gather_evidence(task="t", agent_id="main", audit_records=[])
        assert "git_diff_stat" not in e


class TestHeuristicScoring:
    def setup_method(self):
        eng = MagicMock()
        eng.config.model = "gpt-4o"
        eng.llm = None
        self.ev = EvaluatorAgent(eng)

    def test_clean_run_high_scores(self):
        evidence = {
            "tool_calls": 10,
            "errors": [],
            "permission_decisions": {"allow": 10, "ask": 0, "deny": 0},
        }
        scores, findings, suggestions = self.ev._score_heuristic(evidence)
        completion = next(s for s in scores if s.dimension == "completion")
        assert completion.score == 10.0

    def test_errors_lower_completion(self):
        evidence = {
            "tool_calls": 5,
            "errors": [{"tool": "x", "error": "e"}] * 4,
            "permission_decisions": {"allow": 0, "ask": 0, "deny": 0},
        }
        scores, _, _ = self.ev._score_heuristic(evidence)
        completion = next(s for s in scores if s.dimension == "completion")
        assert completion.score < 10.0

    def test_denies_lower_security(self):
        evidence = {
            "tool_calls": 5,
            "errors": [],
            "permission_decisions": {"allow": 0, "ask": 0, "deny": 3},
        }
        scores, _, _ = self.ev._score_heuristic(evidence)
        security = next(s for s in scores if s.dimension == "security")
        assert security.score < 10.0

    def test_findings_suggestions_emitted_when_relevant(self):
        evidence = {
            "tool_calls": 5,
            "errors": [{"tool": "x", "error": "e"}] * 5,
            "permission_decisions": {"allow": 0, "ask": 0, "deny": 2},
        }
        _, findings, suggestions = self.ev._score_heuristic(evidence)
        assert any("error" in f.lower() for f in findings)
        assert any("error" in s.lower() for s in suggestions)

    def test_all_four_dimensions_returned(self):
        scores, _, _ = self.ev._score_heuristic(
            {"tool_calls": 1, "errors": [], "permission_decisions": {}}
        )
        dims = {s.dimension for s in scores}
        assert dims == set(DIMENSIONS)


class TestEvaluateFallback:
    @pytest.mark.asyncio
    async def test_no_llm_uses_heuristic(self):
        eng = MagicMock()
        eng.config.model = "gpt-4o"
        eng.llm = None
        ev = EvaluatorAgent(eng)
        report = await ev.evaluate(task="t", agent_id="main", audit_records=[])
        assert len(report.scores) == 4
        assert report.task == "t"


class TestEvaluateLLM:
    @pytest.mark.asyncio
    async def test_llm_json_parsed(self):
        eng = MagicMock()
        eng.config.model = "gpt-4o"
        eng.llm = MagicMock()
        canned = json.dumps(
            {
                "scores": [
                    {"dimension": "completion", "score": 9, "rationale": "AC met"},
                    {"dimension": "code_quality", "score": 8, "rationale": "clean"},
                    {"dimension": "security", "score": 7, "rationale": "good"},
                    {"dimension": "performance", "score": 9, "rationale": "fast"},
                ],
                "findings": ["✅ tests pass"],
                "suggestions": ["Add docs"],
            }
        )
        eng.llm.chat = AsyncMock(return_value=(canned, {}))
        ev = EvaluatorAgent(eng)
        report = await ev.evaluate(task="t", agent_id="main", audit_records=[])
        assert report.overall_score == pytest.approx(8.25, abs=0.1)
        assert report.findings == ["✅ tests pass"]
        assert report.suggestions == ["Add docs"]

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back(self):
        eng = MagicMock()
        eng.config.model = "gpt-4o"
        eng.llm = MagicMock()
        eng.llm.chat = AsyncMock(side_effect=RuntimeError("llm down"))
        ev = EvaluatorAgent(eng)
        report = await ev.evaluate(task="t", agent_id="main", audit_records=[])
        # Heuristic returns 4 scores
        assert len(report.scores) == 4

    @pytest.mark.asyncio
    async def test_partial_dimensions_filled(self):
        eng = MagicMock()
        eng.config.model = "gpt-4o"
        eng.llm = MagicMock()
        # LLM only returned 2 dimensions
        canned = json.dumps(
            {
                "scores": [
                    {"dimension": "completion", "score": 9, "rationale": "ok"},
                    {"dimension": "code_quality", "score": 8, "rationale": "ok"},
                ],
            }
        )
        eng.llm.chat = AsyncMock(return_value=(canned, {}))
        ev = EvaluatorAgent(eng)
        report = await ev.evaluate(task="t", agent_id="main", audit_records=[])
        dims = {s.dimension for s in report.scores}
        assert dims == set(DIMENSIONS)  # Missing ones were filled


# ── Report writing ───────────────────────────────────────────────


class TestWriteReport:
    def test_writes_both_files(self, tmp_path):
        r = EvaluationReport(
            task="t",
            agent_id="main",
            scores=[EvaluationScore("completion", 9)],
        )
        md_path, json_path = EvaluatorAgent.write_report(r, workspace=tmp_path)
        assert md_path.exists()
        assert json_path.exists()
        assert md_path.name == "SCORE.md"
        assert json_path.name == ".score.json"

    def test_files_have_correct_content(self, tmp_path):
        r = EvaluationReport(
            task="impl X",
            agent_id="main",
            scores=[EvaluationScore("completion", 9, "AC met")],
        )
        md_path, json_path = EvaluatorAgent.write_report(r, workspace=tmp_path)
        md = md_path.read_text()
        assert "impl X" in md
        assert "9.0/10" in md
        parsed = json.loads(json_path.read_text())
        assert parsed["task"] == "impl X"

    def test_creates_dir_if_missing(self, tmp_path):
        target = tmp_path / "new" / "subdir"
        r = EvaluationReport(task="t", agent_id="main", scores=[])
        md_path, _ = EvaluatorAgent.write_report(r, workspace=target)
        assert md_path.parent.exists()


# ── JSON parser ──────────────────────────────────────────────────


class TestParseScoreResponse:
    def test_plain_json(self):
        d = _parse_score_response('{"scores":[{"dimension":"x","score":8}]}')
        assert d["scores"][0]["dimension"] == "x"

    def test_fenced_json(self):
        text = '```json\n{"scores":[{"dimension":"x","score":8}]}\n```'
        d = _parse_score_response(text)
        assert d["scores"][0]["score"] == 8

    def test_fenced_no_lang(self):
        text = '```\n{"scores":[{"dimension":"x","score":8}]}\n```'
        d = _parse_score_response(text)
        assert "scores" in d

    def test_smart_quotes(self):
        text = '{"scores":[{"dimension":"x","score":8,"rationale":"it’s ok"}]}'
        d = _parse_score_response(text)
        assert "rationale" in d["scores"][0]

    def test_garbage_returns_empty(self):
        assert _parse_score_response("not json at all") == {}
        assert _parse_score_response("") == {}
        assert _parse_score_response(None) == {}

    def test_extracts_from_prose(self):
        text = 'Here is the result: {"scores":[{"dimension":"x","score":8}]} thanks!'
        d = _parse_score_response(text)
        assert "scores" in d


# ── P13-5: Engine integration — run_with_evaluator + cross-family judge ──


class TestRunWithEvaluator:
    """P13-5: engine.run_with_evaluator() wraps run_stream() + writes SCORE.md."""

    def test_picks_alternate_model_for_gpt_main(self):
        """When main is GPT, evaluator defaults to Claude (cross-family)."""
        from agent.agents.evaluator import EvaluatorAgent

        engine = MagicMock()
        engine.config.model = "gpt-4o"
        engine.llm = None
        ev = EvaluatorAgent(engine)
        assert "claude" in ev.model.lower()

    def test_picks_alternate_model_for_claude_main(self):
        """When main is Claude, evaluator defaults to GPT (cross-family)."""
        from agent.agents.evaluator import EvaluatorAgent

        engine = MagicMock()
        engine.config.model = "claude-sonnet-4-6"
        engine.llm = None
        ev = EvaluatorAgent(engine)
        assert "gpt" in ev.model.lower()

    def test_engine_has_run_with_evaluator_method(self):
        """AgentEngine.run_with_evaluator() must be a public method."""
        import inspect

        from agent.core.engine import AgentEngine

        assert hasattr(AgentEngine, "run_with_evaluator")
        sig = inspect.signature(AgentEngine.run_with_evaluator)
        assert "task" in sig.parameters
        assert "plan_context" in sig.parameters

    @pytest.mark.asyncio
    async def test_run_with_evaluator_writes_score_md(self, tmp_path):
        """End-to-end: stub run_stream → verify SCORE.md written to workspace."""
        from agent.core.engine import AgentConfig, AgentEngine

        async def fake_run_stream(task, plan_context=""):
            yield {"type": "tool_call", "tool_name": "read_file", "tool_args": {"path": "x"}}
            yield {"type": "tool_result", "tool_name": "read_file", "success": True}
            yield {
                "type": "final",
                "content": "done",
            }

        e = AgentEngine(AgentConfig(model="mock", provider="mock", tdd_mode="off"))
        e.run_stream = fake_run_stream  # stub
        report = await e.run_with_evaluator(task="hello", workspace=tmp_path)
        # When evaluator can't load an LLM, it falls back to heuristic which
        # always returns 4 scores. So `report` should not be None.
        assert report is not None
        # Files written
        score_md = tmp_path / "SCORE.md"
        score_json = tmp_path / ".score.json"
        assert score_md.exists()
        assert score_json.exists()
        # Content sanity
        assert "hello" in score_md.read_text()
