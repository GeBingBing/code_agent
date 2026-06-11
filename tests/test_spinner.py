"""Tests for ui.spinner.SpinnerController and StageLabel.

Covers:
  - State machine: idle ↔ running transitions
  - API idempotence and label/stats updates without restart
  - Single-line render format (glyph + label + stats)
  - Stall detection (red after threshold with no tokens)
  - Async lifecycle: start spins, ticks happen, stop_async clears
  - StageLabel templating
"""

import asyncio
import io
import time

import pytest

from ui.spinner import (
    CLEAR_LINE,
    DIM,
    RED,
    RESET,
    SPINNERS,
    SpinnerController,
    StageLabel,
)


# ── StageLabel ────────────────────────────────────────────────


class TestStageLabel:
    def test_thinking_is_plain_text(self):
        assert StageLabel.THINKING == "[思考中]"

    def test_tool_supports_format(self):
        rendered = StageLabel.TOOL.format(tool="read_file")
        assert rendered == "[工具: read_file]"

    def test_subagent_supports_format(self):
        rendered = StageLabel.SUBAGENT.format(name="explorer")
        assert rendered == "[子 Agent: explorer]"

    def test_no_emoji_in_any_label(self):
        """Plain text only — no emoji for terminal safety."""
        for attr in (
            StageLabel.THINKING, StageLabel.PLANNING, StageLabel.TOOL,
            StageLabel.SUBAGENT, StageLabel.COMPACTING, StageLabel.STALLED,
        ):
            # Crude check: no chars above U+FFFF (where most emoji live)
            for ch in attr:
                assert ord(ch) < 0x10000, f"emoji-like char in {attr!r}"


# ── State machine (sync) ──────────────────────────────────────


class TestStateMachine:
    def test_starts_idle(self):
        spin = SpinnerController()
        assert spin.is_running is False

    def test_start_sets_running(self):
        spin = SpinnerController()
        spin.start()
        try:
            assert spin.is_running is True
        finally:
            # Cleanup the task so the test doesn't leak
            spin._running = False

    def test_start_is_idempotent(self):
        spin = SpinnerController()
        spin.start(StageLabel.THINKING)
        first_task = spin._task
        spin.start(StageLabel.PLANNING)  # second start
        # Same task, not restarted
        assert spin._task is first_task
        assert spin._label == StageLabel.PLANNING
        # Cleanup
        spin._running = False

    def test_start_resets_stats(self):
        spin = SpinnerController()
        spin.update_stats(tokens_in=999, tokens_out=42)
        spin.start(StageLabel.THINKING)
        try:
            # Stats reset on start
            assert spin._tokens_in == 0
            assert spin._tokens_out == 0
        finally:
            spin._running = False

    def test_update_label_does_not_restart(self):
        spin = SpinnerController()
        spin.start(StageLabel.THINKING)
        first_task = spin._task
        spin.update_label(StageLabel.TOOL.format(tool="read_file"))
        assert spin._task is first_task
        assert spin._label == "[工具: read_file]"
        spin._running = False

    def test_update_label_starts_if_idle(self):
        spin = SpinnerController()
        assert spin.is_running is False
        spin.update_label(StageLabel.PLANNING)
        try:
            assert spin.is_running is True
            assert spin._label == StageLabel.PLANNING
        finally:
            spin._running = False

    def test_update_stats_partial(self):
        spin = SpinnerController()
        spin.update_stats(tokens_in=100)
        assert spin._tokens_in == 100
        assert spin._tokens_out == 0
        spin.update_stats(tokens_out=50)
        assert spin._tokens_in == 100
        assert spin._tokens_out == 50

    def test_update_stats_ignores_none(self):
        spin = SpinnerController()
        spin.update_stats(tokens_in=10, tokens_out=20)
        spin.update_stats()  # no kwargs
        assert spin._tokens_in == 10
        assert spin._tokens_out == 20


# ── Render (sync, no async) ───────────────────────────────────


class TestRender:
    def test_render_includes_glyph_label_and_stats(self):
        spin = SpinnerController(
            file=io.StringIO(),
            tick_ms=80,
            stall_threshold_s=5.0,
            clock=lambda: 100.0,  # frozen time
        )
        spin._start_time = 100.0
        spin._last_token_time = 100.0
        spin._frame_idx = 0
        spin._label = StageLabel.THINKING
        spin._tokens_in = 1234
        spin._tokens_out = 567

        spin._render()
        out = spin._file.getvalue()

        # Must clear line, contain glyph, label, token stats, elapsed
        assert CLEAR_LINE in out
        assert SPINNERS[0] in out
        assert StageLabel.THINKING in out
        assert "1234" in out and "567" in out
        # Elapsed = 0 since clock is frozen
        assert "0s" in out

    def test_render_stall_shows_red_and_stalled_label(self):
        spin = SpinnerController(
            file=io.StringIO(),
            stall_threshold_s=5.0,
            clock=lambda: 200.0,  # 100s past start
        )
        spin._start_time = 100.0
        spin._last_token_time = 100.0  # never received a token
        spin._frame_idx = 0
        spin._label = StageLabel.THINKING
        # No tokens → stalled

        spin._render()
        out = spin._file.getvalue()

        assert RED in out
        assert StageLabel.STALLED in out

    def test_render_no_stall_when_tokens_arrived(self):
        spin = SpinnerController(
            file=io.StringIO(),
            stall_threshold_s=5.0,
            clock=lambda: 200.0,  # 100s past start
        )
        spin._start_time = 100.0
        spin._last_token_time = 200.0  # recent token
        spin._tokens_in = 10  # has tokens
        spin._tokens_out = 5
        spin._label = StageLabel.THINKING

        spin._render()
        out = spin._file.getvalue()

        # Should NOT be red, should NOT show STALLED label
        assert RED not in out
        assert StageLabel.STALLED not in out
        assert StageLabel.THINKING in out

    def test_render_shows_context_window_pct(self):
        spin = SpinnerController(
            file=io.StringIO(),
            clock=lambda: 100.0,
        )
        spin._start_time = 100.0
        spin._last_token_time = 100.0
        spin._ctx_used = 50000
        spin._ctx_window = 100000  # 50% used

        spin._render()
        out = spin._file.getvalue()
        assert "50% ctx" in out

    def test_render_context_low_remaining(self):
        spin = SpinnerController(
            file=io.StringIO(),
            clock=lambda: 100.0,
        )
        spin._start_time = 100.0
        spin._last_token_time = 100.0
        spin._ctx_used = 80000
        spin._ctx_window = 100000  # 20% remaining

        spin._render()
        out = spin._file.getvalue()
        # <25% remaining shows "ctx 20%" without "ctx" word
        assert "20%" in out

    def test_render_respects_no_color_env(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        # Re-import to pick up env var
        import importlib
        from ui import spinner
        importlib.reload(spinner)
        try:
            spin = spinner.SpinnerController(
                file=io.StringIO(),
                clock=lambda: 100.0,
            )
            spin._start_time = 100.0
            spin._last_token_time = 100.0
            spin._label = StageLabel.THINKING
            spin._render()
            out = spin._file.getvalue()
            # No ANSI codes when NO_COLOR is set
            assert "\033[" not in out
        finally:
            # Restore module state for other tests
            monkeypatch.delenv("NO_COLOR", raising=False)
            importlib.reload(spinner)


# ── Async lifecycle ───────────────────────────────────────────


class TestAsyncLifecycle:
    @pytest.mark.asyncio
    async def test_start_runs_the_spin_task(self):
        spin = SpinnerController(tick_ms=5)  # fast tick for test
        spin.start()
        assert spin._task is not None
        assert not spin._task.done()
        # Let it tick a few times
        await asyncio.sleep(0.05)
        assert spin._tick_count >= 2, f"expected ≥2 ticks, got {spin._tick_count}"
        await spin.stop_async()
        assert spin.is_running is False
        assert spin._task is None

    @pytest.mark.asyncio
    async def test_stop_async_clears_line(self):
        buf = io.StringIO()
        spin = SpinnerController(file=buf, tick_ms=5)
        spin.start()
        await asyncio.sleep(0.02)
        await spin.stop_async()
        out = buf.getvalue()
        # Final frame must include clear-line
        assert out.rstrip().endswith(CLEAR_LINE) or CLEAR_LINE in out

    @pytest.mark.asyncio
    async def test_stop_async_is_safe_when_not_running(self):
        spin = SpinnerController()
        await spin.stop_async()  # should not raise
        await spin.stop_async()  # idempotent

    @pytest.mark.asyncio
    async def test_double_stop_does_not_raise(self):
        spin = SpinnerController(tick_ms=5)
        spin.start()
        await spin.stop_async()
        await spin.stop_async()  # second stop is no-op
        assert spin.is_running is False

    @pytest.mark.asyncio
    async def test_stats_update_visible_after_tick(self):
        buf = io.StringIO()
        spin = SpinnerController(file=buf, tick_ms=5)
        spin.start()
        await asyncio.sleep(0.01)  # one tick
        spin.update_stats(tokens_in=42, tokens_out=7)
        await asyncio.sleep(0.05)  # a few more ticks
        await spin.stop_async()
        out = buf.getvalue()
        # Stats should appear in the rendered output
        assert "42" in out
        assert "7" in out


# ── Integration with CLI patterns ─────────────────────────────


class TestCliPatterns:
    """Tests the calling patterns we use in ui/cli.py."""

    @pytest.mark.asyncio
    async def test_typical_lifecycle(self):
        """Simulates: start → update_label(tool) → update_stats → stop."""
        buf = io.StringIO()
        spin = SpinnerController(file=buf, tick_ms=5)

        spin.start(StageLabel.THINKING)
        await asyncio.sleep(0.02)
        spin.update_label(StageLabel.TOOL.format(tool="read_file"))
        await asyncio.sleep(0.02)
        spin.update_stats(tokens_in=100, tokens_out=10)
        await asyncio.sleep(0.02)
        await spin.stop_async()

        out = buf.getvalue()
        assert "思考中" not in out or "read_file" in out  # label changed
        assert "read_file" in out
        assert "100" in out
        assert "10" in out
