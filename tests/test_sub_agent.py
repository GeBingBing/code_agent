"""Tests for Phase 5: Sub-agent tool."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.sub_agent import SpawnSubAgentTool
from agent.core.subagent_registry import reset_registry


def _run(async_fn):
    return asyncio.run(async_fn)


class TestSpawnSubAgent:
    def setup_method(self):
        # Reset registry before each test
        reset_registry()

    def test_spawns_and_returns_result(self, monkeypatch):
        tool = SpawnSubAgentTool()

        # Mock the inner AgentEngine.run to avoid real LLM calls
        async def fake_run(self, task):
            return f"Mock result for: {task}"

        monkeypatch.setattr(
            "agent.core.engine.AgentEngine.run", fake_run
        )

        result = _run(tool.execute(task="write hello.py"))
        assert result.success is True
        assert "Mock result for: write hello.py" in result.content
        assert "[Sub-agent" in result.content and "result]" in result.content
