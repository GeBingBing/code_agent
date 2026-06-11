"""Tests for the Ralph TDD supervisor (PR-02)."""

import pytest

from agent.core.tdd_ralph import RalphSupervisor
from agent.core.tdd_state_machine import InvalidTDDTransition, TDDState, TDDStateMachine


class TestRalphBlocksInRedState:
    @pytest.mark.asyncio
    async def test_cannot_write_implementation_in_red(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("write_file", {"path": "src/auth.py"})
        assert violation is not None
        assert "RED" in violation

    @pytest.mark.asyncio
    async def test_cannot_apply_diff_to_implementation_in_red(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("apply_diff", {"path": "src/auth.py"})
        assert violation is not None

    @pytest.mark.asyncio
    async def test_can_write_test_in_red(self):
        """The whole point of RED is to write a failing test — that's allowed."""
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("write_file", {"path": "tests/test_auth.py"})
        assert violation is None


class TestRalphAllowsInGreenState:
    @pytest.mark.asyncio
    async def test_can_write_implementation_in_green(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        sm.transition(TDDState.GREEN)
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("write_file", {"path": "src/auth.py"})
        assert violation is None


class TestRalphBlocksRefactorInWrongState:
    @pytest.mark.asyncio
    async def test_cannot_refactor_in_red(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("refactor", {"target": "src/auth.py"})
        assert violation is not None
        assert "REFACTOR" in violation

    @pytest.mark.asyncio
    async def test_cannot_refactor_in_green(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        sm.transition(TDDState.GREEN)
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("refactor", {"target": "src/auth.py"})
        assert violation is not None

    @pytest.mark.asyncio
    async def test_can_refactor_in_refactor_state(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        sm.transition(TDDState.GREEN)
        sm.transition(TDDState.REFACTOR)
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("refactor", {"target": "src/auth.py"})
        assert violation is None


class TestRalphModeBehavior:
    @pytest.mark.asyncio
    async def test_strict_returns_violation_message(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("write_file", {"path": "src/x.py"})
        assert isinstance(violation, str)

    @pytest.mark.asyncio
    async def test_guided_returns_none_with_warning(self):
        sm = TDDStateMachine(mode="guided")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("write_file", {"path": "src/x.py"})
        # Guided mode warns but doesn't block
        assert violation is None

    @pytest.mark.asyncio
    async def test_off_mode_noop(self):
        sm = TDDStateMachine(mode="off")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("write_file", {"path": "src/x.py"})
        assert violation is None

    @pytest.mark.asyncio
    async def test_no_active_cycle_noop(self):
        sm = TDDStateMachine(mode="strict")
        # No cycle started
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("write_file", {"path": "src/x.py"})
        assert violation is None


class TestRalphPathDetection:
    @pytest.mark.asyncio
    async def test_tests_directory_is_not_implementation(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("write_file", {"path": "tests/unit/test_x.py"})
        assert violation is None

    @pytest.mark.asyncio
    async def test_nested_tests_directory(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        violation = await ralph.check("write_file", {"path": "src/tests/test_x.py"})
        # /tests/ substring matches
        assert violation is None

    @pytest.mark.asyncio
    async def test_python_file_is_implementation(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        assert await ralph.check("write_file", {"path": "x.py"}) is not None

    @pytest.mark.asyncio
    async def test_typescript_file_is_implementation(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        assert await ralph.check("write_file", {"path": "x.ts"}) is not None

    @pytest.mark.asyncio
    async def test_non_code_file_not_implementation(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        # .md is not code
        assert await ralph.check("write_file", {"path": "README.md"}) is None

    @pytest.mark.asyncio
    async def test_empty_path_not_implementation(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        assert await ralph.check("write_file", {}) is None


class TestRalphTransition:
    def test_transition_proxies_to_sm(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        ralph.transition(TDDState.GREEN)
        assert sm.current_state == TDDState.GREEN

    def test_transition_strict_propagates_exception(self):
        sm = TDDStateMachine(mode="strict")
        sm.start_cycle("feature")
        ralph = RalphSupervisor(sm)
        with pytest.raises(InvalidTDDTransition):
            ralph.transition(TDDState.REFACTOR)  # Skip
