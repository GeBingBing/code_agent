"""Tests for Phase 1: Memory system (L1/L2/L3)."""

import tempfile

from agent.core.memory import MemoryManager


class TestWorkingMemory:
    """Test L1 working memory."""

    def test_add_and_get_messages(self):
        mem = MemoryManager()
        mem.add("user", "Hello")
        mem.add("assistant", "Hi there")
        msgs = mem.get_messages()
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[0].content == "Hello"

    def test_estimate_tokens(self):
        mem = MemoryManager()
        mem.add("user", "x" * 400)
        # With tiktoken: ~50 tokens; without: ~133 + 4 overhead = 137
        est = mem._estimate_tokens()
        assert 45 <= est <= 140, f"Expected 45-140, got {est}"

    def test_tool_call_id_preserved(self):
        mem = MemoryManager()
        mem.add("tool", "result", tool_call_id="call_123")
        msgs = mem.get_messages()
        assert msgs[0].tool_call_id == "call_123"


class TestCompression:
    """Test L2 session summary compression."""

    def test_compress_when_over_limit(self):
        mem = MemoryManager(max_tokens=10)  # very low for testing
        # Each message is 50 chars ~ 12 tokens. After 7 messages, total ~ 84 > 10
        # and working_memory has 7 items (> keep=6), so compression triggers.
        for i in range(8):
            mem.add("user", f"msg{i:02d}" + "x" * 46)
        # Should have compressed older messages into summary
        assert len(mem.summaries) > 0
        # Working memory should keep recent 6
        assert len(mem.working_memory) <= 6

    def test_get_messages_includes_summaries(self):
        mem = MemoryManager(max_tokens=10)
        for i in range(8):
            mem.add("user", f"msg{i:02d}" + "x" * 46)
        msgs = mem.get_messages()
        # First messages should be summary system messages
        assert any("Earlier summary" in m.content for m in msgs)


class TestLongTermMemory:
    """Test L3 long-term memory persistence."""

    def test_remember_and_persist(self, tmp_path):
        mem_dir = tmp_path / ".test-memory"
        mem = MemoryManager(memory_dir=str(mem_dir))
        mem.remember("framework", "pytest")
        assert "pytest" in mem.long_term
        # Verify file was written
        assert (mem_dir / "memory.md").exists()

    def test_load_existing_memory(self, tmp_path):
        mem_dir = tmp_path / ".test-memory"
        mem_dir.mkdir()
        (mem_dir / "memory.md").write_text("- framework: pytest\n")
        mem = MemoryManager(memory_dir=str(mem_dir))
        assert "pytest" in mem.long_term

    def test_get_long_term_context(self):
        mem = MemoryManager(memory_dir=tempfile.mkdtemp())
        assert mem.get_long_term_context() == ""
        mem.remember("test", "value")
        ctx = mem.get_long_term_context()
        assert "Long-term memory" in ctx
        assert "test" in ctx
