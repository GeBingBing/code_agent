"""Ralph supervisor — MoAI-ADK Ralph engine pattern (PR-02).

Detects TDD sequence violations and blocks bad tool calls. Designed to be
registered as a hook on BEFORE_TOOL_EXECUTION (PR-01).

Rules enforced in `strict` mode:
  - Cannot write implementation files while in RED state
  - Cannot refactor (or commit) while in RED/GREEN state
  - Cannot skip a state transition

In `guided` mode, violations log warnings but the tool call proceeds.
In `off` mode, Ralph is a no-op.
"""

import logging
from typing import Optional

from .tdd_state_machine import TDDState, TDDStateMachine, InvalidTDDTransition

logger = logging.getLogger(__name__)


# Tool names that write implementation code (not tests)
_IMPLEMENTATION_TOOLS = frozenset({
    "write_file",
    "apply_diff",
    "insert_after_line",
    "replace_lines",
})


class RalphSupervisor:
    """Watches tool calls and enforces TDD sequence.

    Pass the engine's hook registry an async check function that returns
    an error message (None if OK) for any tool call that violates the cycle.
    """

    def __init__(self, state_machine: TDDStateMachine):
        self.sm = state_machine

    async def check(self, tool_name: str, args: dict) -> Optional[str]:
        """Return an error message if the tool call violates TDD sequence.

        Returns None if the call is allowed.
        """
        if self.sm.mode == "off" or not self.sm.is_active:
            return None

        # Rule 1: Cannot write implementation in RED state
        if (tool_name in _IMPLEMENTATION_TOOLS
                and self.sm.current_state == TDDState.RED):
            target = self._extract_target(args)
            if self._looks_like_implementation(target):
                return self._violation(
                    f"Cannot {tool_name} '{target}' in RED state. "
                    f"Write a failing test first (use write_failing_test or run_tests). "
                    f"Current state: RED — expecting failing test."
                )

        # Rule 2: Cannot run refactor in non-REFACTOR state
        if tool_name in ("refactor", "git_commit") and self.sm.current_state != TDDState.REFACTOR:
            return self._violation(
                f"Tool '{tool_name}' only allowed in REFACTOR state, "
                f"but current state is {self.sm.current_state.value}."
            )

        return None

    def transition(self, next_state: TDDState) -> None:
        """Proxy to state machine — propagates strict-mode exceptions."""
        self.sm.transition(next_state)

    def _extract_target(self, args: dict) -> str:
        return args.get("path", args.get("file", args.get("target", "")))

    def _looks_like_implementation(self, path: str) -> bool:
        """A path looks like implementation if it's not under tests/."""
        if not path:
            return False
        if path.startswith("tests/") or "/tests/" in path or path.endswith("test_*.py"):
            return False
        return path.endswith((".py", ".js", ".ts", ".go", ".rs", ".java"))

    def _violation(self, message: str) -> str:
        if self.sm.mode == "strict":
            # Strict mode: surface the message as a TDD violation
            return message
        # Guided mode: warn but don't block
        logger.warning("TDD guidance: %s", message)
        return None
