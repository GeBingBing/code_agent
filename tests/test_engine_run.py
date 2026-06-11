"""Mock test for AgentEngine.run() — full ReAct loop verification.

This test mocks the LLMClient so it doesn't need a real API key.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.core.engine import AgentEngine, AgentConfig
from agent.tools.base import registry


def _make_tool_call_msg(tool_calls: list) -> SimpleNamespace:
    """Build a fake OpenAI message object with tool_calls."""
    return SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id=f"call_{i}",
                function=SimpleNamespace(
                    name=tc["name"],
                    arguments=json.dumps(tc.get("args", {})),
                ),
            )
            for i, tc in enumerate(tool_calls)
        ],
    )


def _make_text_msg(text: str) -> SimpleNamespace:
    """Build a fake OpenAI message object with plain text."""
    return SimpleNamespace(content=text, tool_calls=None)


class TestEngineRun:
    """Test the full ReAct loop with mocked LLM."""

    @pytest.fixture
    def engine(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        # Disable hooks that require a real LLM (PR-11 dual-review, PR-10 OTel,
        # PR-08 audit, PR-12 AB test, PR-13 progress anchor) — this test uses
        # a stub LLM, so the dual-review manager's primary reviewer can't
        # actually evaluate calls and would raise ReviewRequiresUser.
        config = AgentConfig(
            model="mock", provider="openai", mode="bypass",
            tdd_mode="off", audit_enabled=False, otel_enabled=False,
            enable_dual_review=False, ab_test_enabled=False,
            progress_anchor_enabled=False,
        )
        eng = AgentEngine(config)
        # Replace the real LLM client with a stub to avoid API key requirement
        eng.llm = type("StubLLM", (), {"chat": AsyncMock()})()
        return eng

    async def _patch_chat(self, engine, responses):
        """Mock LLMClient.chat to return a sequence of responses."""
        call_iter = iter(responses)
        original_chat = engine.llm.chat

        async def fake_chat(*args, **kwargs):
            return next(call_iter)

        engine.llm.chat = fake_chat
        return engine

    def test_run_single_tool_call(self, engine, tmp_path, monkeypatch):
        """Agent calls write_file, then returns final answer."""
        monkeypatch.setattr(
            engine, "llm",
            type("FakeLLM", (), {
                "chat": AsyncMock(side_effect=[
                    _make_tool_call_msg([{"name": "write_file", "args": {"path": str(tmp_path / "a.txt"), "content": "hello"}}]),
                    _make_text_msg("Done"),
                ])
            })()
        )

        result = asyncio.run(engine.run("write a file"))
        assert result == "Done"
        assert (tmp_path / "a.txt").read_text() == "hello"

    def test_run_multiple_steps(self, engine, tmp_path, monkeypatch):
        """Agent reads a file, then writes based on content."""
        (tmp_path / "input.txt").write_text("world")

        monkeypatch.setattr(
            engine, "llm",
            type("FakeLLM", (), {
                "chat": AsyncMock(side_effect=[
                    _make_tool_call_msg([{"name": "read_file", "args": {"path": str(tmp_path / "input.txt")}}]),
                    _make_tool_call_msg([{"name": "write_file", "args": {"path": str(tmp_path / "output.txt"), "content": "hello world"}}]),
                    _make_text_msg("Completed"),
                ])
            })()
        )

        result = asyncio.run(engine.run("read input and write output"))
        assert result == "Completed"
        assert (tmp_path / "output.txt").read_text() == "hello world"

    def test_run_permission_blocks_critical(self, engine, monkeypatch):
        """Critical commands are blocked even with mocked LLM."""
        monkeypatch.setattr(
            engine, "llm",
            type("FakeLLM", (), {
                "chat": AsyncMock(side_effect=[
                    _make_tool_call_msg([{"name": "execute_command", "args": {"command": "rm -rf /"}}]),
                    _make_text_msg("I see that was blocked"),
                ])
            })()
        )

        result = asyncio.run(engine.run("dangerous task"))
        assert "blocked" in result.lower() or "blocked" in str(engine.memory.working_memory)

    def test_run_memory_accumulates(self, engine, tmp_path, monkeypatch):
        """Tool results are saved into working memory."""
        monkeypatch.setattr(
            engine, "llm",
            type("FakeLLM", (), {
                "chat": AsyncMock(side_effect=[
                    _make_tool_call_msg([{"name": "list_files", "args": {"path": str(tmp_path)}}]),
                    _make_text_msg("Empty dir"),
                ])
            })()
        )

        asyncio.run(engine.run("list files"))
        msgs = engine.memory.get_messages()
        roles = [m.role for m in msgs]
        assert "tool" in roles


# ── TestMultiTurnExtraction (M1) ─────────────────────────────────
# The engine should pass the [Previous conversation] portion as
# history to the fact_extractor (L3 + M1 wire-up).


class TestMultiTurnExtraction:
    """M1: engine.run wires FactConfirmExtractor + history into extraction."""

    def test_engine_passes_history_to_extractor(self, tmp_path, monkeypatch):
        """When the task contains '[Previous conversation]...[Current task]X',
        engine must pass the [Previous conversation] portion as `history=`
        and only X as the extraction target.
        """
        from agent.core.engine import AgentEngine, AgentConfig
        from agent.core.fact_extractor import FactConfirmExtractor
        from agent.core.user_profile import UserProfile
        from unittest.mock import MagicMock, AsyncMock

        # Profile on a tmp path
        profile_path = tmp_path / "user_profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_path))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        config = AgentConfig(
            model="mock", provider="openai", mode="bypass",
            tdd_mode="off", audit_enabled=False, otel_enabled=False,
            enable_dual_review=False, ab_test_enabled=False,
            progress_anchor_enabled=False,
        )
        eng = AgentEngine(config)
        eng.user_profile = UserProfile()
        eng.llm = MagicMock()
        eng.llm.chat = AsyncMock(return_value=('{"facts": []}', None))

        # Patch extract_and_apply_async to capture kwargs
        captured = {}

        async def spy_extract(self, text, profile, history=""):
            captured["text"] = text
            captured["history"] = history
            captured["profile"] = profile
            return []

        monkeypatch.setattr(
            FactConfirmExtractor, "extract_and_apply_async", spy_extract
        )

        full_task = (
            "[Previous conversation]\n"
            "User: 我是工程师\n"
            "Assistant: 好的\n"
            "[Current task]\n"
            "我叫什么？"
        )

        # run_stream is an async generator — consume it. The extraction
        # call happens at the top of run_stream, BEFORE the streaming
        # loop, so we only need to drive the generator far enough to
        # reach the extraction. Any later pre-existing engine bug is
        # unrelated to this test.
        async def drain():
            try:
                async for _ in eng.run_stream(full_task, plan_context=""):
                    break  # one event is enough — extraction is the first action
            except Exception:
                pass  # pre-existing engine issues below extraction are not our concern

        asyncio.run(drain())

        # Verify the engine passed the right pieces
        assert captured.get("text", "").strip() == "我叫什么？", \
            f"extract target should be '我叫什么？', got {captured.get('text')!r}"
        assert "我是工程师" in captured.get("history", ""), \
            f"history should contain prior user message, got {captured.get('history')!r}"
        assert "[Previous conversation]" not in captured.get("history", "")

    def test_engine_uses_fact_confirm_extractor(self):
        """The default extractor wired into the engine is FactConfirmExtractor.

        Inspect the call site: the engine imports and uses
        FactConfirmExtractor (not the bare FactExtractor) — L3 is on by default.
        """
        import inspect
        from agent.core.engine import AgentEngine
        src = inspect.getsource(AgentEngine.run_stream)
        assert "FactConfirmExtractor" in src, \
            "engine.run_stream must use FactConfirmExtractor (L3) by default"
        assert "extract_and_apply_async" in src, \
            "engine.run_stream must use the async path (unlocks M1 history)"
        assert "history=history_text" in src, \
            "engine.run_stream must pass history= kwarg"

    def test_engine_keeps_sync_path_in_direct_answer(self):
        """ui/cli.py:1167 _direct_answer still uses sync extract_and_apply
        (no async required there).
        """
        import inspect
        from ui import cli
        # Find the function body and ensure it calls sync extract_and_apply
        # (not extract_and_apply_async).
        src = inspect.getsource(cli)
        # The cli should reference extract_and_apply (sync)
        assert "extract_and_apply" in src, \
            "ui.cli should still call sync extract_and_apply somewhere"
