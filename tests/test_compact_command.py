"""Tests for the /compact slash command."""

import pytest

from agent.commands.base import registry


class TestCompactRegistration:
    def test_compact_registered(self):
        import agent.commands.builtin  # noqa: F401

        assert "compact" in registry._commands

    def test_compress_alias_registered(self):
        import agent.commands.builtin  # noqa: F401

        cmd = registry._commands["compact"]
        assert "compress" in cmd.aliases


class TestCompactHandler:
    @pytest.mark.asyncio
    async def test_no_engine_returns_warning(self):
        import agent.commands.builtin  # noqa: F401

        handler = registry._commands["compact"]._handler
        result = await handler("", ctx={})
        assert "No active engine" in result or "engine" in result.lower()

    @pytest.mark.asyncio
    async def test_compact_folds_older_messages(self):
        """A loaded working memory should shrink after /compact."""
        import agent.commands.builtin  # noqa: F401
        from agent.core.memory import MemoryManager

        # Build a fake engine with a memory holding many messages.
        mem = MemoryManager(max_tokens=2000)
        for i in range(20):
            mem.add("user", f"Message number {i} " * 20)  # bulk to exceed budget
            mem.add("assistant", f"Reply {i} " * 20)

        engine = type("E", (), {"memory": mem})()
        handler = registry._commands["compact"]._handler
        result = await handler("", ctx={"engine": engine})

        # Working memory must have shrunk (older messages folded into summaries).
        assert len(mem.working_memory) < 40, result
        # The handler reports a compaction.
        assert "compacted" in result.lower() or "compact" in result.lower()
