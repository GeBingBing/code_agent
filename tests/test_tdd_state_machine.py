"""Tests for the TDD state machine (PR-02)."""

import pytest

from agent.core.tdd_state_machine import (
    InvalidTDDTransition,
    TDDCycle,
    TDDState,
    TDDStateMachine,
)


class TestTDDStateMachineInit:
    def test_default_mode_is_guided(self):
        sm = TDDStateMachine()
        assert sm.mode == "guided"

    def test_strict_mode_accepted(self):
        sm = TDDStateMachine(mode="strict")
        assert sm.mode == "strict"

    def test_off_mode_accepted(self):
        sm = TDDStateMachine(mode="off")
        assert sm.mode == "off"

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid TDD mode"):
            TDDStateMachine(mode="invalid")


class TestTDDStateMachineTransitions:
    def test_legal_transition_red_to_green(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        sm.transition(TDDState.GREEN)
        assert sm.current_state == TDDState.GREEN

    def test_legal_transition_green_to_refactor(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        sm.transition(TDDState.GREEN)
        sm.transition(TDDState.REFACTOR)
        assert sm.current_state == TDDState.REFACTOR

    def test_legal_transition_refactor_to_done(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        sm.transition(TDDState.GREEN)
        sm.transition(TDDState.REFACTOR)
        sm.transition(TDDState.DONE)
        assert sm.current_state == TDDState.DONE

    def test_skip_in_strict_mode_raises(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        with pytest.raises(InvalidTDDTransition, match="red.*green"):
            sm.transition(TDDState.REFACTOR)

    def test_skip_in_strict_mode_raises_2(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        sm.transition(TDDState.GREEN)
        with pytest.raises(InvalidTDDTransition, match="green.*refactor"):
            sm.transition(TDDState.DONE)

    def test_skip_in_guided_mode_warns_but_proceeds(self):
        sm = TDDStateMachine(mode="guided")
        sm.start_cycle("feature")
        sm.transition(TDDState.DONE)  # Skips GREEN and REFACTOR
        assert sm.current_state == TDDState.DONE
        # Violation was recorded
        assert len(sm.cycle.violations) > 0

    def test_off_mode_allows_anything(self):
        sm = TDDStateMachine(mode="off")
        sm.start_cycle("feature")
        sm.transition(TDDState.REFACTOR)  # Skip allowed
        sm.transition(TDDState.DONE)  # Skip allowed
        assert sm.current_state == TDDState.DONE
        # No violations recorded in off mode
        assert len(sm.cycle.violations) == 0


class TestTDDCycleRecording:
    def test_record_red(self):
        sm = TDDStateMachine()
        sm.start_cycle("feature")
        sm.record_red("tests/test_x.py", {"passed": 0, "failed": 1})
        assert sm.cycle.test_path == "tests/test_x.py"
        assert sm.cycle.test_red_run == {"passed": 0, "failed": 1}

    def test_record_green(self):
        sm = TDDStateMachine()
        sm.start_cycle("feature")
        sm.record_green("src/x.py", {"passed": 1, "failed": 0})
        assert sm.cycle.impl_path == "src/x.py"
        assert sm.cycle.test_green_run == {"passed": 1, "failed": 0}

    def test_record_refactor(self):
        sm = TDDStateMachine()
        sm.start_cycle("feature")
        sm.record_refactor()
        sm.record_refactor("abc123")
        assert len(sm.cycle.refactor_commits) == 2
        assert sm.cycle.refactor_commits[1] == "abc123"

    def test_record_methods_safe_without_active_cycle(self):
        sm = TDDStateMachine()
        sm.record_red("x", {})  # No cycle — should be no-op
        sm.record_green("x", {})
        sm.record_refactor()
        # No error


class TestCycleLifecycle:
    def test_start_cycle_replaces_previous(self):
        sm = TDDStateMachine()
        sm.start_cycle("first")
        first = sm.cycle
        sm.start_cycle("second")
        assert sm.cycle != first
        assert sm.cycle.feature == "second"
        assert len(sm.history) == 1
        assert sm.history[0] is first

    def test_reset_clears_cycle(self):
        sm = TDDStateMachine()
        sm.start_cycle("feature")
        sm.reset()
        assert sm.cycle is None

    def test_is_active(self):
        sm = TDDStateMachine()
        assert not sm.is_active
        sm.start_cycle("feature")
        assert sm.is_active
        sm.transition(TDDState.GREEN)
        sm.transition(TDDState.REFACTOR)
        sm.transition(TDDState.DONE)
        assert not sm.is_active


class TestSummary:
    def test_summary_includes_mode(self):
        sm = TDDStateMachine(mode="strict")
        s = sm.summary()
        assert s["mode"] == "strict"
        assert s["active"] is False

    def test_summary_active(self):
        sm = TDDStateMachine()
        sm.start_cycle("my feature")
        s = sm.summary()
        assert s["active"] is True
        assert s["current_state"] == "red"
        assert s["current_feature"] == "my feature"
        assert s["completed_cycles"] == 0

    def test_summary_after_completion(self):
        sm = TDDStateMachine()
        sm.start_cycle("a")
        sm.transition(TDDState.GREEN)
        sm.transition(TDDState.REFACTOR)
        sm.transition(TDDState.DONE)
        sm.start_cycle("b")
        s = sm.summary()
        assert s["completed_cycles"] == 1


class TestTDDCycleDict:
    def test_to_dict(self):
        cycle = TDDCycle(feature="test", state=TDDState.RED)
        d = cycle.to_dict()
        assert d["feature"] == "test"
        assert d["state"] == "red"
        assert d["test_path"] is None
        assert d["violations"] == []
