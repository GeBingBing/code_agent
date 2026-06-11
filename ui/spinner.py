"""Shared spinner + status line controller for the CLI.

Replaces 3 inline copies previously in `_direct_answer`, `_run_edit`,
`_run_task` of `ui/cli.py`. A single `SpinnerController` instance per CLI
runs an asyncio task that re-renders one line at 80ms ticks; stage
transitions come from `update_label()` / `update_stats()` calls.

Why single-line (not the previous 2-line layout):
  - Single `\r\033[K` is self-contained; doesn't need to track cursor
    position across readline/prompt boundaries.
  - 2-line layouts drift if any other code prints to stdout between
    spinner ticks.
  - The lost shimmer effect is a small visual regression; we keep
    the stall-detection color (red after 5s with no token).

Color/no-color: respects `$NO_COLOR`. The CLI's `CLEAR_LINE` /
`RESET` / `DIM` constants are duplicated here intentionally to keep
`spinner.py` standalone (avoiding circular import: cli → spinner → cli).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import IO, Optional

# ── ANSI constants (mirror ui/cli.py to keep this module standalone) ──

_NO_COLOR = os.environ.get("NO_COLOR", "") != ""
if _NO_COLOR:
    RESET = BOLD = DIM = YELLOW = RED = ""
    CLEAR_LINE = ""
else:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CLEAR_LINE = "\r\033[K"


# Spinner glyphs (reused from ui/cli.py:80, kept in sync)
SPINNERS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class StageLabel:
    """Plain-text stage labels. No emoji — terminal-safe.

    `{tool}` / `{name}` placeholders are filled by the caller:
        spin.update_label(StageLabel.TOOL.format(tool="read_file"))
    """

    THINKING = "[思考中]"
    PLANNING = "[规划中]"
    TOOL = "[工具: {tool}]"
    SUBAGENT = "[子 Agent: {name}]"
    COMPACTING = "[压缩上下文]"
    STALLED = "[等待中]"


class SpinnerController:
    """Single shared spinner. One instance per CLI session.

    Lifecycle: idle → running (via start) → idle (via stop_async).
    While running, the asyncio task ticks every `tick_ms` milliseconds
    and re-renders the same line. `update_label()` / `update_stats()`
    mutate the next render; the task picks them up on the next tick.

    The controller is **synchronous in API surface** for the common
    case (start/update/stop) because the spinning task is fire-and-
    forget. `stop_async()` must be awaited to ensure the final
    `\\r\\033[K` is written before the next thing prints to stdout.

    Example:
        spin = SpinnerController()
        spin.start()
        spin.update_label(StageLabel.TOOL.format(tool="read_file"))
        spin.update_stats(tokens_in=1234, tokens_out=567)
        await spin.stop_async()
    """

    DEFAULT_TICK_MS = 80
    DEFAULT_STALL_S = 5.0

    def __init__(
        self,
        file: Optional[IO[str]] = None,
        tick_ms: int = DEFAULT_TICK_MS,
        stall_threshold_s: float = DEFAULT_STALL_S,
        clock: Optional[callable] = None,
    ) -> None:
        """Args:
        file: Output stream (default sys.stdout). Tests can pass a
            StringIO to capture writes.
        tick_ms: Animation tick interval in milliseconds.
        stall_threshold_s: Seconds without a token update before
            the spinner turns red (signals "model is taking long").
        clock: Callable returning current monotonic time. Defaults
            to `time.monotonic`. Tests can inject a fake clock.
        """
        self._file = file or sys.stdout
        self._tick_s = tick_ms / 1000.0
        self._stall_threshold_s = stall_threshold_s
        self._clock = clock or time.monotonic
        self._task: Optional[asyncio.Task] = None
        self._label: str = StageLabel.THINKING
        self._tokens_in: int = 0
        self._tokens_out: int = 0
        self._ctx_used: int = 0
        self._ctx_window: int = 0
        self._running: bool = False
        self._start_time: float = 0.0
        self._last_token_time: float = 0.0
        self._frame_idx: int = 0
        # Test instrumentation
        self._tick_count: int = 0

    # ── Public API ───────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, label: str = StageLabel.THINKING) -> None:
        """Begin spinning. Idempotent — calling twice does nothing.

        If the spinner is already running, this updates the label
        rather than restarting (preserves stats).
        """
        if self._running:
            self._label = label
            return
        self._label = label
        self._tokens_in = 0
        self._tokens_out = 0
        self._ctx_used = 0
        self._ctx_window = 0
        self._frame_idx = 0
        self._tick_count = 0
        self._start_time = self._clock()
        self._last_token_time = self._start_time
        self._running = True
        # create_task requires a running loop. In async callers (CLI),
        # the loop is always present. In sync tests, we silently skip
        # task creation — the state still flips to running so callers
        # can assert on `is_running`; the render() and stop_async()
        # paths are tested separately with a running loop.
        try:
            asyncio.get_running_loop()
            self._task = asyncio.create_task(self._spin())
        except RuntimeError:
            # No running event loop — animation disabled but state set
            self._task = None

    def update_label(self, label: str) -> None:
        """Change the label without restarting. If not running, starts."""
        if not self._running:
            self.start(label)
        else:
            self._label = label

    def update_stats(
        self,
        *,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        ctx_used: Optional[int] = None,
        ctx_window: Optional[int] = None,
    ) -> None:
        """Update stats shown in the metadata section.

        All kwargs optional — pass only what changed. Tokens > 0 reset
        the stall timer (model is producing again).
        """
        if tokens_in is not None:
            self._tokens_in = tokens_in
        if tokens_out is not None:
            self._tokens_out = tokens_out
        if ctx_used is not None:
            self._ctx_used = ctx_used
        if ctx_window is not None:
            self._ctx_window = ctx_window
        if (self._tokens_in > 0) or (self._tokens_out > 0):
            self._last_token_time = self._clock()

    async def stop_async(self) -> None:
        """Stop the spinner and clear the line. Must be awaited."""
        if not self._running:
            return
        self._running = False
        if self._task is not None and not self._task.done():
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        # Final clear so no glyph is left on the line
        try:
            self._file.write(f"\r{CLEAR_LINE}")
            self._file.flush()
        except (ValueError, OSError):
            pass  # file may be closed during shutdown

    # ── Internal ─────────────────────────────────────────────────

    async def _spin(self) -> None:
        """Main render loop. Runs until `self._running` flips False."""
        try:
            while self._running:
                self._render()
                self._frame_idx = (self._frame_idx + 1) % len(SPINNERS)
                self._tick_count += 1
                await asyncio.sleep(self._tick_s)
        except asyncio.CancelledError:
            # Normal shutdown path
            pass

    def _render(self) -> None:
        """Build the single-line status and write it. Synchronous."""
        try:
            now = self._clock()
            elapsed = now - self._start_time
            since_token = now - self._last_token_time

            glyph = SPINNERS[self._frame_idx]
            parts = [f"{elapsed:.0f}s"]
            if self._tokens_in or self._tokens_out:
                parts.append(f"⬇ {self._tokens_in} / {self._tokens_out}")
            if self._ctx_used and self._ctx_window:
                pct = self._ctx_used / self._ctx_window * 100
                remaining = max(0, 100 - pct)
                if remaining < 25:
                    parts.append(f"ctx {remaining:.0f}%")
                elif remaining < 40:
                    parts.append(f"ctx {remaining:.0f}%")
                else:
                    parts.append(f"{pct:.0f}% ctx")

            # Stall color: red if no token for >threshold AND no tokens yet
            has_tokens = bool(self._tokens_in or self._tokens_out)
            stalled = since_token > self._stall_threshold_s and not has_tokens
            label_to_show = StageLabel.STALLED if stalled else self._label
            color = RED if stalled else DIM
            stats = f"  ·  {' · '.join(parts)}" if parts else ""

            line = f"\r{CLEAR_LINE}{color}{glyph} {label_to_show}{stats}{RESET}"
            self._file.write(line)
            self._file.flush()
        except (ValueError, OSError):
            # stdout closed during shutdown — silent
            pass
