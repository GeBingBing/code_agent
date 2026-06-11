"""Tests for the pinned memory feature (PR-14)."""

import pytest

from agent.core.memory import MemoryManager


@pytest.fixture
def tmp_memory_dir(tmp_path):
    """Override the default ~/.coding-agent dir to a tmp path."""
    return tmp_path


# ── TestBasicPinning ──────────────────────────────────────


class TestBasicPinning:
    def test_pinned_marker_appears_in_file(self, tmp_memory_dir):
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        m.remember("user_name", "hay", pinned=True)
        assert "📌" in m.long_term
        assert "user_name: hay" in m.long_term

    def test_unpinned_no_marker(self, tmp_memory_dir):
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        m.remember("last_written_file", "/tmp/x.py", pinned=False)
        assert "📌" not in m.long_term
        assert "last_written_file: /tmp/x.py" in m.long_term

    def test_pinned_persists_across_reload(self, tmp_memory_dir):
        m1 = MemoryManager(memory_dir=str(tmp_memory_dir))
        m1.remember("user_name", "hay", pinned=True)
        # The remember() call auto-saves to disk
        m2 = MemoryManager(memory_dir=str(tmp_memory_dir))
        # New instance should see the pinned entry
        assert "📌" in m2.long_term
        assert "user_name: hay" in m2.long_term

    def test_pinned_field_detected(self, tmp_memory_dir):
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        m.remember("user_name", "hay", pinned=True)
        m.remember("last_command", "ls", pinned=False)
        # Find the user_name line
        user_line = next(l for l in m.long_term.split("\n") if "user_name" in l)
        assert m.is_pinned(user_line) is True
        cmd_line = next(l for l in m.long_term.split("\n") if "last_command" in l)
        assert m.is_pinned(cmd_line) is False


# ── TestPinnedNotEvicted ─────────────────────────────────


class TestPinnedNotEvicted:
    def test_pinned_survives_past_50_unpinned(self, tmp_memory_dir):
        """The original bug: 50-entry cap would evict user facts."""
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        # Add a pinned user fact
        m.remember("user_name", "hay", pinned=True)
        # Add 60 unpinned tool-flood entries (with different keys)
        for i in range(60):
            m.remember(f"tool_call_{i}", f"call_{i}", pinned=False)
        # The pinned user_name should still be there
        assert "user_name: hay" in m.long_term
        assert "📌" in m.long_term

    def test_pinned_evicted_only_at_pinned_max(self, tmp_memory_dir):
        """Pinned entries have their own higher cap (default 200)."""
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        m._PINNED_MAX = 5
        for i in range(10):
            m.remember(f"pinned_{i}", f"v_{i}", pinned=True)
        # Should be capped at 5
        pinned_lines = [l for l in m.long_term.split("\n") if "pinned_" in l and l.strip()]
        assert len(pinned_lines) == 5
        # The most recent ones should be kept
        assert "pinned_9" in m.long_term
        assert "pinned_5" in m.long_term
        assert "pinned_0" not in m.long_term

    def test_pinned_and_unpinned_caps_independent(self, tmp_memory_dir):
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        # Add 50 unpinned (at the cap, different keys)
        for i in range(50):
            m.remember(f"tool_call_{i}", f"call_{i}", pinned=False)
        # Add 100 pinned (under the 200 cap, different keys)
        for i in range(100):
            m.remember(f"pinned_fact_{i}", f"fact_{i}", pinned=True)
        # All 100 pinned should survive
        pinned_count = sum(1 for l in m.long_term.split("\n") if "pinned_fact_" in l and l.strip())
        assert pinned_count == 100


# ── TestPinnedUpdateReplaces ─────────────────────────────


class TestPinnedUpdateReplaces:
    def test_pinned_update_replaces_old_value(self, tmp_memory_dir):
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        m.remember("user_name", "old", pinned=True)
        m.remember("user_name", "new", pinned=True)
        # Only one entry with user_name should exist
        name_lines = [l for l in m.long_term.split("\n") if "user_name" in l and l.strip()]
        assert len(name_lines) == 1
        assert "new" in name_lines[0]
        assert "old" not in m.long_term

    def test_pinned_to_unpinned_swap(self, tmp_memory_dir):
        """If you re-remember an unpinned key that was pinned, replaces."""
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        m.remember("user_name", "hay", pinned=True)
        m.remember("user_name", "hay_v2", pinned=False)
        # Should still be one entry, now unpinned
        name_lines = [l for l in m.long_term.split("\n") if "user_name" in l and l.strip()]
        assert len(name_lines) == 1
        assert "📌" not in name_lines[0]


# ── TestMultilineSanitize ────────────────────────────────


class TestMultilineSanitize:
    def test_multiline_value_sanitized(self, tmp_memory_dir):
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        m.remember("test", "line1\nline2\nline3")
        # The \n should be replaced with ↵
        assert "↵" in m.long_term
        # The file should still be valid one-line-per-entry
        lines = [l for l in m.long_term.strip().split("\n") if l]
        for line in lines:
            assert line.startswith("- ")

    def test_control_chars_stripped(self, tmp_memory_dir):
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        m.remember("test", "before\x00after")
        # Null byte should be stripped
        assert "\x00" not in m.long_term
        assert "beforeafter" in m.long_term

    def test_empty_value_ignored(self, tmp_memory_dir):
        m = MemoryManager(memory_dir=str(tmp_memory_dir))
        m.remember("test", "")
        # Empty value should not be stored
        assert "- test:" not in m.long_term
        # The header "- " is for entries; "test:" alone should not be a line
        test_lines = [
            l for l in m.long_term.split("\n") if l.strip().startswith("-") and "test:" in l
        ]
        assert test_lines == []
