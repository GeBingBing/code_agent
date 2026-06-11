"""Tests for engine ↔ Evaluator integration (PR-09)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.engine import AgentEngine, AgentConfig
from agent.core.audit_log import reset_audit_logger, get_audit_logger


@pytest.fixture
def isolated_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_AGENT_AUDIT_DIR", str(tmp_path / "audit"))
    reset_audit_logger()
    yield get_audit_logger()
    reset_audit_logger()


class TestEvaluateCommand:
    @pytest.mark.asyncio
    async def test_no_engine_returns_warning(self):
        from agent.commands.builtin import _handle_evaluate
        out = await _handle_evaluate("", {"engine": None})
        assert "No engine" in out

    @pytest.mark.asyncio
    async def test_with_engine_no_llm_uses_heuristic(self, isolated_audit, tmp_path, monkeypatch):
        """When no LLM is configured, /evaluate should still succeed (heuristic mode)."""
        from agent.commands.builtin import _handle_evaluate
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        # Seed audit log so evaluator has evidence
        isolated_audit.log({
            "session_id": "s", "agent_id": "main",
            "action": "tool_call", "tool": "read_file",
        })
        # Direct workspace to tmp_path so we don't pollute the project
        e._workspace = tmp_path
        out = await _handle_evaluate("test task", {"engine": e})
        assert "evaluated" in out.lower()
        assert "SCORE.md" in out
        assert (tmp_path / "SCORE.md").exists()
        assert (tmp_path / ".score.json").exists()

    @pytest.mark.asyncio
    async def test_score_json_is_valid(self, isolated_audit, tmp_path):
        from agent.commands.builtin import _handle_evaluate
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        e._workspace = tmp_path
        await _handle_evaluate("test", {"engine": e})
        data = json.loads((tmp_path / ".score.json").read_text())
        assert "scores" in data
        assert len(data["scores"]) == 4
        dims = {s["dimension"] for s in data["scores"]}
        assert dims == {"completion", "code_quality", "security", "performance"}

    @pytest.mark.asyncio
    async def test_score_md_format(self, isolated_audit, tmp_path):
        from agent.commands.builtin import _handle_evaluate
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        e._workspace = tmp_path
        await _handle_evaluate("build feature X", {"engine": e})
        md = (tmp_path / "SCORE.md").read_text()
        assert "# Task Evaluation" in md
        assert "build feature X" in md
        assert "## Scores" in md
        assert "总分" in md

    @pytest.mark.asyncio
    async def test_command_registered(self):
        from agent.commands.base import registry
        cmd = registry.get("evaluate")
        assert cmd is not None
        assert "Evaluator" in cmd.description or "evaluator" in cmd.description.lower()


class TestEvaluatorReadsAudit:
    @pytest.mark.asyncio
    async def test_evaluator_finds_audit_records(self, isolated_audit, tmp_path):
        """The evaluator should pick up records from the singleton audit log."""
        from agent.agents.evaluator import EvaluatorAgent
        # Seed
        for i in range(5):
            isolated_audit.log({
                "session_id": "s", "agent_id": "main",
                "action": "tool_call", "tool": "read_file",
            })
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        evaluator = EvaluatorAgent(e)
        records = isolated_audit.query(agent_id="main")
        report = await evaluator.evaluate(
            task="test task", agent_id="main",
            audit_records=records,
            workspace=tmp_path,
        )
        # Evidence should reflect what we logged
        assert report.task == "test task"
        assert len(report.scores) == 4


class TestEvaluatorWithFakeLLM:
    @pytest.mark.asyncio
    async def test_uses_engine_llm_when_available(self, isolated_audit, tmp_path):
        """Evaluator should call engine.llm.chat when one is configured."""
        from agent.agents.evaluator import EvaluatorAgent
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        # Inject fake LLM
        canned = json.dumps({
            "scores": [
                {"dimension": "completion", "score": 9, "rationale": "AC met"},
                {"dimension": "code_quality", "score": 7, "rationale": "clean"},
                {"dimension": "security", "score": 8, "rationale": "safe"},
                {"dimension": "performance", "score": 6, "rationale": "ok"},
            ],
            "findings": ["all tests pass"],
            "suggestions": ["add rate limiting"],
        })
        e.llm = MagicMock()
        e.llm.chat = AsyncMock(return_value=(canned, {}))

        evaluator = EvaluatorAgent(e)
        report = await evaluator.evaluate(
            task="impl auth", agent_id="main",
            audit_records=[], workspace=tmp_path,
        )
        e.llm.chat.assert_called_once()
        assert report.overall_score == pytest.approx(7.5, abs=0.1)
        assert "all tests pass" in report.findings
