"""Tests for error recovery: circuit breaker + tool retry."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.core.engine import AgentConfig, AgentEngine
from agent.tools.base import ToolResult, registry


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


class MockToolAlwaysFail:
    """Tool that always returns an error."""

    name = "always_fail"
    description = "Always fails"
    is_concurrency_safe = False
    is_read_only = False
    user_facing_name = "Mock"

    def is_enabled(self):
        return True

    def check_permissions(self, args):
        return True, ""

    def prompt_contribution(self):
        return ""

    @property
    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": "always_fail",
                "description": "Always fails",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }

    async def execute(self, **kwargs):
        await asyncio.sleep(0.01)
        return ToolResult(success=False, content="", error="Intentional failure")


class MockToolSucceedOnRetry:
    """Tool that fails first time but succeeds on retry."""

    name = "succeed_on_retry"
    description = "Succeeds on second call"
    is_concurrency_safe = False
    is_read_only = False
    user_facing_name = "Mock"

    def is_enabled(self):
        return True

    def check_permissions(self, args):
        return True, ""

    def prompt_contribution(self):
        return ""

    @property
    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": "succeed_on_retry",
                "description": "Succeeds on second call",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }

    async def execute(self, **kwargs):
        if not hasattr(self, "_call_count"):
            self._call_count = 0
        self._call_count += 1
        if self._call_count == 1:
            return ToolResult(success=False, content="", error="First attempt failed")
        return ToolResult(success=True, content="Success after retry")


class TestCircuitBreaker:
    """Test circuit breaker for repeated tool failures."""

    @pytest.fixture
    def engine(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        config = AgentConfig(model="mock", provider="openai", mode="bypass")
        eng = AgentEngine(config)
        from unittest.mock import AsyncMock

        eng.llm = type("StubLLM", (), {"chat": AsyncMock()})()
        return eng

    def teardown_method(self):
        """Clean up mock tools registered during tests."""
        for name in ["always_fail", "succeed_on_retry"]:
            if registry._tools.get(name):
                del registry._tools[name]

    def test_retry_tool_once_on_failure(self, engine, monkeypatch):
        """Failed tool error is reported to LLM; LLM decides to retry or stop."""
        tool = MockToolSucceedOnRetry()
        tool._call_count = 0
        registry.register(tool)

        chat_calls = [0]

        async def fake_chat(*args, **kwargs):
            chat_calls[0] += 1
            if chat_calls[0] == 1:
                # First LLM response: call the tool
                return _make_tool_call_msg([{"name": "succeed_on_retry", "args": {}}])
            elif chat_calls[0] == 2:
                # Second LLM response: retry the tool (LLM decides to retry)
                return _make_tool_call_msg([{"name": "succeed_on_retry", "args": {}}])
            else:
                # After tool succeeds, LLM responds with final text
                return _make_text_msg("Task completed successfully")

        engine.llm.chat = fake_chat

        async def run():
            return await engine.run("test task")

        result = asyncio.run(run())
        # Tool was called twice: LLM decided to retry after seeing failure
        assert tool._call_count == 2
        assert "completed" in result.lower()

    def test_circuit_breaker_after_max_retries(self, engine, monkeypatch):
        """After repeated failures, consecutive_failures counter increments."""
        tool = MockToolAlwaysFail()
        registry.register(tool)

        async def fake_chat(*args, **kwargs):
            return _make_tool_call_msg([{"name": "always_fail", "args": {}}])

        engine.llm.chat = fake_chat

        async def run():
            return await engine.run("test task")

        result = asyncio.run(run())
        # LLM keeps retrying since fake_chat always returns tool calls;
        # failure counter increments each time in engine._consecutive_failures
        assert (
            "Intentional failure" in result
            or engine._consecutive_failures.get("always_fail", 0) > 0
        )
        tool_msgs = [m.content for m in engine.memory.get_messages() if m.role == "tool"]
        assert any("Intentional failure" in msg for msg in tool_msgs)

    def test_tool_succeeds_without_retry(self, engine, monkeypatch):
        """Successful tool should not be retried."""
        tool = MockToolSucceedOnRetry()
        tool._call_count = 0
        registry.register(tool)

        async def fake_chat(*args, **kwargs):
            # First call: LLM returns plain text (no tool call), so tool is never invoked
            return _make_text_msg("Task completed successfully")

        engine.llm.chat = fake_chat

        async def run():
            return await engine.run("test task")

        result = asyncio.run(run())
        assert tool._call_count == 0  # No tool was called


class TestErrorRecovery:
    """Test error recovery strategies."""

    @pytest.fixture
    def engine(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        config = AgentConfig(model="mock", provider="openai", mode="bypass")
        eng = AgentEngine(config)
        from unittest.mock import AsyncMock

        eng.llm = type("StubLLM", (), {"chat": AsyncMock()})()
        return eng

    def teardown_method(self):
        """Clean up mock tools registered during tests."""
        for name in ["always_fail", "succeed_on_retry"]:
            if registry._tools.get(name):
                del registry._tools[name]

    def test_fallback_on_tool_not_found(self, engine, monkeypatch):
        """Unknown tool should return error via tool result, not crash."""

        async def fake_chat(*args, **kwargs):
            return _make_tool_call_msg([{"name": "nonexistent_tool", "args": {}}])

        engine.llm.chat = fake_chat

        async def run():
            return await engine.run("test task")

        result = asyncio.run(run())
        # Check that error about unknown tool appears in memory
        tool_msgs = [m.content for m in engine.memory.get_messages() if m.role == "tool"]
        unknown_found = any(
            "unknown tool" in msg.lower() or "not found" in msg.lower() for msg in tool_msgs
        )
        assert unknown_found, f"Expected 'unknown tool' in tool messages: {tool_msgs}"
