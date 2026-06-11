"""TDD state machine — enforces Red → Green → Refactor sequence (PR-02).

Unlike the prompt-level TDD suggestion in P0-3, this is a strict state machine
that tracks cycle progress and refuses to skip states when in `strict` mode.

States:
    RED        — write a failing test
    GREEN      — write implementation to make test pass
    REFACTOR   — clean up code while keeping tests green
    DONE       — terminal

Transitions are linear; any other transition is invalid in `strict` mode.
In `guided` mode, invalid transitions log a warning but proceed.
In `off` mode, the state machine is a no-op.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TDDState(Enum):
    """TDD cycle states."""

    RED = "red"
    GREEN = "green"
    REFACTOR = "refactor"
    DONE = "done"


# Legal state transitions
_TRANSITIONS = {
    TDDState.RED: TDDState.GREEN,
    TDDState.GREEN: TDDState.REFACTOR,
    TDDState.REFACTOR: TDDState.DONE,
    TDDState.DONE: None,  # terminal
}


class InvalidTDDTransition(Exception):
    """Raised when an illegal state transition is attempted in strict mode."""

    pass


@dataclass
class TDDCycle:
    """A single TDD cycle for one feature/AC.

    Tracks where we are in the cycle and what artifacts have been produced.
    """

    feature: str
    state: TDDState = TDDState.RED
    test_path: Optional[str] = None
    impl_path: Optional[str] = None
    test_red_run: Optional[dict] = None  # {"passed": int, "failed": int, "passed": bool}
    test_green_run: Optional[dict] = None
    refactor_commits: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "feature": self.feature,
            "state": self.state.value,
            "test_path": self.test_path,
            "impl_path": self.impl_path,
            "test_red_run": self.test_red_run,
            "test_green_run": self.test_green_run,
            "refactor_commits": self.refactor_commits,
            "violations": self.violations,
        }


class TDDStateMachine:
    """Enforces Red → Green → Refactor sequence.

    Modes:
        strict  — illegal transitions raise InvalidTDDTransition
        guided  — log a warning but allow
        off     — no-op (default for backward compatibility)
    """

    def __init__(self, mode: str = "guided"):
        if mode not in ("strict", "guided", "off"):
            raise ValueError(f"Invalid TDD mode: {mode!r} (must be strict|guided|off)")
        self.mode = mode
        self.cycle: Optional[TDDCycle] = None
        self.history: list[TDDCycle] = []

    def start_cycle(self, feature: str) -> None:
        """Begin a new TDD cycle. Resets state to RED."""
        if self.cycle is not None:
            self.history.append(self.cycle)
        self.cycle = TDDCycle(feature=feature, state=TDDState.RED)

    def transition(self, next_state: TDDState) -> None:
        """Move to next_state. Behavior depends on mode.

        In strict mode: raises InvalidTDDTransition for illegal moves.
        In guided mode: logs warning for illegal moves, still transitions.
        In off mode: just transitions (no validation, no record).
        """
        if self.cycle is None:
            return

        if self.mode == "off":
            # Off: just update state, no validation
            self.cycle.state = next_state
            return

        current = self.cycle.state
        allowed = _TRANSITIONS.get(current)
        if next_state != allowed:
            msg = f"Cannot transition {current.value} → {next_state.value} (expected: {allowed.value if allowed else 'terminal'})"
            self.cycle.violations.append(msg)
            if self.mode == "strict":
                raise InvalidTDDTransition(msg)
            # guided: warn but proceed
            import logging

            logging.getLogger(__name__).warning("TDD skip: %s", msg)
        self.cycle.state = next_state

    def record_red(self, test_path: str, test_result: dict) -> None:
        """Record the RED-step test result (expecting failure)."""
        if self.cycle is None:
            return
        self.cycle.test_path = test_path
        self.cycle.test_red_run = test_result
        # If test unexpectedly passed in RED, that's a hint that the test
        # is not actually testing the feature. We don't auto-fail here;
        # the LLM is expected to interpret this.

    def record_green(self, impl_path: str, test_result: dict) -> None:
        """Record the GREEN-step implementation + passing test result."""
        if self.cycle is None:
            return
        self.cycle.impl_path = impl_path
        self.cycle.test_green_run = test_result

    def record_refactor(self, commit_hash: str = "") -> None:
        """Record a refactor commit."""
        if self.cycle is None:
            return
        self.cycle.refactor_commits.append(
            commit_hash or f"refactor@{len(self.cycle.refactor_commits)+1}"
        )

    def reset(self) -> None:
        """Clear the current cycle (does not affect history)."""
        self.cycle = None

    @property
    def current_state(self) -> Optional[TDDState]:
        return self.cycle.state if self.cycle else None

    @property
    def is_active(self) -> bool:
        return self.cycle is not None and self.cycle.state != TDDState.DONE

    def summary(self) -> dict:
        """Snapshot for /status command."""
        return {
            "mode": self.mode,
            "active": self.is_active,
            "current_state": self.current_state.value if self.current_state else None,
            "current_feature": self.cycle.feature if self.cycle else None,
            "completed_cycles": len(self.history),
            "violations_in_current": len(self.cycle.violations) if self.cycle else 0,
        }
