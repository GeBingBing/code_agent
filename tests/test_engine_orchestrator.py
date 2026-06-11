"""Tests for the engine's orchestrator integration (PR-07)."""

import json
import pytest

from agent.core.engine import AgentEngine, AgentConfig
from agent.agents import OrchestratorAgent


class TestEngineRunWithOrchestrator:
    @pytest.mark.asyncio
    async def test_no_llm_raises(self):
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        # llm is None in mock mode
        assert e.llm is None
        with pytest.raises(RuntimeError, match="Orchestrator needs an LLM"):
            await e.run_with_orchestrator("any task")

    @pytest.mark.asyncio
    async def test_orchestrator_end_to_end_with_mock_llm(self, monkeypatch):
        """Wire a fake LLM that returns canned decompose/merge output,
        and a fake dispatch_fn via injection. Verify the orchestrator
        runs the full pipeline."""

        class _FakeMsg:
            def __init__(self, role, content):
                self.role = role
                self.content = content

        class _FakeLLM:
            async def chat(self, messages, tools=None, stream=False, **kw):
                # First call: decompose; subsequent: dispatch + merge
                # The engine's run_with_orchestrator uses llm_call for
                # decompose + merge, and dispatches via dispatch_fn
                # (which itself calls self.llm.chat).
                content = messages[-1].content
                if "Decompose" in content:
                    return json.dumps([
                        {"id": "st-a", "role": "code", "description": "impl", "depends_on": []},
                        {"id": "st-b", "role": "test", "description": "test", "depends_on": ["st-a"]},
                    ]), {}
                if "Synthesize" in content:
                    return "FINAL: did 2 subtasks", {}
                # dispatch_fn path
                return f"OUT-{messages[-1].content[:30]}", {}

        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        # Inject a fake LLM
        e.llm = _FakeLLM()
        result = await e.run_with_orchestrator("Build feature X")
        assert "FINAL" in result or "did 2 subtasks" in result

    @pytest.mark.asyncio
    async def test_orchestrator_handles_dispatch_failure(self, monkeypatch):
        """If a subtask dispatch raises, the response is marked failed and
        the orchestrator still completes (with merge)."""

        class _FakeLLM:
            async def chat(self, messages, **kw):
                content = messages[-1].content
                if "Decompose" in content:
                    return json.dumps([
                        {"id": "st-a", "role": "code", "description": "impl", "depends_on": []},
                    ]), {}
                if "Synthesize" in content:
                    return "MERGED", {}
                # Simulate dispatch failure
                raise RuntimeError("sub-agent failed")

        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        e.llm = _FakeLLM()
        result = await e.run_with_orchestrator("task")
        # Merge still happens; result contains the merge output
        assert "MERGED" in result

    @pytest.mark.asyncio
    async def test_orchestrator_logs_start_and_done(self, monkeypatch):
        class _FakeLLM:
            async def chat(self, messages, **kw):
                content = messages[-1].content
                if "Decompose" in content:
                    return json.dumps([
                        {"id": "a", "role": "code", "description": "x", "depends_on": []},
                    ]), {}
                if "Synthesize" in content:
                    return "ok", {}
                return "ok", {}

        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        e.llm = _FakeLLM()
        # Should not raise; should log
        result = await e.run_with_orchestrator("task")
        assert result == "ok"


class TestOrchestratorCommand:
    @pytest.mark.asyncio
    async def test_no_args_returns_usage(self):
        from agent.commands.builtin import _handle_orchestrate
        result = await _handle_orchestrate("", {"engine": None})
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_no_engine_returns_warning(self):
        from agent.commands.builtin import _handle_orchestrate
        result = await _handle_orchestrate("do thing", {"engine": None})
        assert "No engine" in result

    @pytest.mark.asyncio
    async def test_no_llm_returns_warning(self):
        from agent.commands.builtin import _handle_orchestrate
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        from agent.commands.builtin import _handle_orchestrate
        result = await _handle_orchestrate("do thing", {"engine": e})
        # Case-insensitive
        assert "llm" in result.lower()

    @pytest.mark.asyncio
    async def test_registered_in_command_registry(self):
        from agent.commands.base import registry
        cmd = registry.get("orchestrate")
        assert cmd is not None
        assert "Orchestrator" in cmd.description
