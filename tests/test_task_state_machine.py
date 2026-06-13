"""Tests for the Task state machine (PR-03)."""

import json

import pytest

from agent.core.task_state_machine import (
    InvalidStateTransition,
    TaskState,
    TaskStateMachine,
    TaskStateRecord,
)


@pytest.fixture
def tmp_state_file(tmp_path):
    """Provide a unique state file path for each test."""
    return tmp_path / "task_state.json"


class TestTaskStateRecord:
    def test_round_trip_dict(self):
        rec = TaskStateRecord(
            task="test",
            state="init",
            created_at="2026-06-06T10:00:00",
            updated_at="2026-06-06T10:00:00",
        )
        d = rec.to_dict()
        rec2 = TaskStateRecord.from_dict(d)
        assert rec2.task == "test"
        assert rec2.state == "init"
        assert rec2.completed_steps == []

    def test_from_dict_ignores_unknown_fields(self):
        d = {
            "task": "x",
            "state": "init",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "unknown_field": "ignored",
        }
        rec = TaskStateRecord.from_dict(d)
        assert rec.task == "x"


class TestTaskStateMachineInit:
    def test_default_state_is_init(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        assert sm.state == TaskState.INIT

    def test_creates_empty_record(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        assert sm.record.task == ""
        assert sm.record.completed_steps == []

    def test_loads_existing_state(self, tmp_state_file):
        # Pre-populate file
        existing = {
            "task": "old",
            "state": "exec",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T01:00:00",
            "completed_steps": [{"tool": "read_file"}],
        }
        tmp_state_file.write_text(json.dumps(existing))
        sm = TaskStateMachine(state_file=tmp_state_file)
        assert sm.record.task == "old"
        assert sm.state == TaskState.EXEC
        assert len(sm.record.completed_steps) == 1

    def test_corrupt_file_backed_up(self, tmp_state_file):
        tmp_state_file.write_text("{not valid json")
        sm = TaskStateMachine(state_file=tmp_state_file)
        # Should not crash; corrupt file backed up, init new
        assert sm.state == TaskState.INIT
        # Backup exists
        backups = list(tmp_state_file.parent.glob("task_state.corrupt.*.json"))
        assert len(backups) == 1


class TestTaskStateTransitions:
    def test_init_to_plan(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.transition(TaskState.PLAN)
        assert sm.state == TaskState.PLAN

    def test_full_happy_path(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.transition(TaskState.PLAN)
        sm.transition(TaskState.EXEC)
        sm.transition(TaskState.TEST)
        sm.transition(TaskState.REVIEW)
        sm.transition(TaskState.DONE)
        assert sm.state == TaskState.DONE

    def test_illegal_skip_raises(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        with pytest.raises(InvalidStateTransition, match="init.*exec"):
            sm.transition(TaskState.EXEC)

    def test_can_revert_plan_to_init(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.transition(TaskState.PLAN)
        sm.transition(TaskState.INIT)
        assert sm.state == TaskState.INIT

    def test_can_fail_from_any_state(self, tmp_state_file):
        for from_state in [
            TaskState.INIT,
            TaskState.PLAN,
            TaskState.EXEC,
            TaskState.TEST,
            TaskState.REVIEW,
        ]:
            sm = TaskStateMachine(state_file=tmp_state_file)
            # Force to from_state
            sm.record.state = from_state.value
            sm.transition(TaskState.FAILED)
            assert sm.state == TaskState.FAILED

    def test_can_recover_from_failed(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.record.state = TaskState.FAILED.value
        sm.transition(TaskState.INIT)
        assert sm.state == TaskState.INIT
        sm.transition(TaskState.PLAN)
        assert sm.state == TaskState.PLAN

    def test_cannot_transition_from_done(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.record.state = TaskState.DONE.value
        with pytest.raises(InvalidStateTransition):
            sm.transition(TaskState.EXEC)


class TestPersistence:
    def test_transition_persists_to_disk(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.transition(TaskState.PLAN)
        # Reload
        sm2 = TaskStateMachine(state_file=tmp_state_file)
        assert sm2.state == TaskState.PLAN

    def test_completed_step_persists(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.record_completed_step("read_file", {"path": "x.py"}, "abc123")
        sm2 = TaskStateMachine(state_file=tmp_state_file)
        assert len(sm2.record.completed_steps) == 1
        assert sm2.record.completed_steps[0]["tool"] == "read_file"

    def test_op_hash_changes_with_step(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        initial_hash = sm.record.op_hash
        sm.record_completed_step("read_file", {}, "h1")
        assert sm.record.op_hash != initial_hash
        # Chain: hash of (prev + op) for the next op differs
        sm.record_completed_step("write_file", {}, "h2")
        assert sm.record.op_hash.startswith("sha256:")
        assert len(sm.record.op_hash) > 10


class TestStepRecording:
    def test_record_completed_step_appends(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.record_completed_step("read_file", {"path": "a.py"}, "h1")
        sm.record_completed_step("write_file", {"path": "b.py"}, "h2")
        assert len(sm.record.completed_steps) == 2

    def test_step_summarizes_args(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.record_completed_step("read_file", {"path": "x.py"}, "h1")
        step = sm.record.completed_steps[0]
        assert "args_summary" in step
        assert "x.py" in step["args_summary"]

    def test_long_args_truncated(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        long_args = {"data": "x" * 1000}
        sm.record_completed_step("write", long_args, "h1")
        assert "…" in sm.record.completed_steps[0]["args_summary"]


class TestKnownIssues:
    def test_add_known_issue(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.add_known_issue("rate limiting missing")
        assert "rate limiting missing" in sm.record.known_issues

    def test_duplicate_issue_not_added(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.add_known_issue("issue")
        sm.add_known_issue("issue")
        assert sm.record.known_issues.count("issue") == 1


class TestStartTask:
    def test_start_task_resets(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.record_completed_step("read_file", {}, "h1")
        sm.add_known_issue("old issue")
        sm.start_task("new task", session_id="sess-1")
        assert sm.record.task == "new task"
        assert sm.record.session_id == "sess-1"
        assert sm.record.completed_steps == []
        assert sm.record.known_issues == []


class TestSummary:
    def test_summary_keys(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        s = sm.summary()
        for key in (
            "state",
            "task",
            "completed_steps",
            "current_step",
            "next_step",
            "known_issues",
            "updated_at",
            "session_id",
        ):
            assert key in s

    def test_summary_empty(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        s = sm.summary()
        assert s["state"] == "init"
        assert s["completed_steps"] == 0


class TestFormatReminder:
    def test_empty_task_returns_empty(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        assert sm.format_reminder() == ""

    def test_active_task_reminder(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.start_task("write a fib function", session_id="s1")
        reminder = sm.format_reminder()
        assert "Task State" in reminder
        assert "write a fib function" in reminder
        assert "init" in reminder


class TestDelete:
    def test_delete_removes_file(self, tmp_state_file):
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.transition(TaskState.PLAN)
        assert tmp_state_file.exists()
        sm.delete()
        assert not tmp_state_file.exists()
        assert sm.state == TaskState.INIT


# ── P12-3: Engine integration — verify run_stream transitions FSM ──


class TestEngineFSMIntegration:
    """P12-3: AgentEngine should drive the FSM through PLAN→EXEC→TEST→REVIEW→DONE."""

    def test_helper_swallows_invalid_transition(self, tmp_path, monkeypatch):
        """_task_state_transition must not raise on illegal moves."""
        from agent.core.engine import AgentConfig, AgentEngine

        # Point task state machine at a tmp file
        from agent.core.task_state_machine import TaskStateMachine

        monkeypatch.setattr(TaskStateMachine, "DEFAULT_STATE_FILE", tmp_path / "task_state.json")
        cfg = AgentConfig(model="mock", provider="mock", tdd_mode="off")
        e = AgentEngine(cfg)
        # INIT → DONE is illegal (must go through PLAN/EXEC/TEST/REVIEW)
        # The helper must swallow this without raising.
        e._task_state_transition(TaskState.DONE)

    def test_review_state_hook_transitions_only_for_high_risk(self, tmp_path, monkeypatch):
        """_review_state_transition_hook fires only on high-risk tools."""
        from agent.core.engine import AgentConfig, AgentEngine
        from agent.core.task_state_machine import TaskStateMachine

        monkeypatch.setattr(TaskStateMachine, "DEFAULT_STATE_FILE", tmp_path / "task_state.json")
        cfg = AgentConfig(model="mock", provider="mock", tdd_mode="off")
        e = AgentEngine(cfg)
        # Force-enable dual review (mock mode disables it by default)
        if e.dual_review is None:
            # Inject a minimal dual_review with a high-risk predicate
            from agent.core.dual_review import DualReviewManager

            e.dual_review = DualReviewManager()
        e.task_state_machine.start_task(task="x", session_id="test")
        # Walk through legal transitions to reach EXEC.
        e.task_state_machine.transition(TaskState.PLAN)
        e.task_state_machine.transition(TaskState.EXEC)
        # Low-risk tool → no transition
        import asyncio as _asyncio

        _asyncio.run(e._review_state_transition_hook({"tool": "read_file"}))
        assert e.task_state_machine.state == TaskState.EXEC
        # High-risk tool → transition to REVIEW
        _asyncio.run(e._review_state_transition_hook({"tool": "write_file"}))
        assert e.task_state_machine.state == TaskState.REVIEW


class TestResumeRestoresContext:
    """P12-3: --resume must restore completed_steps + known_issues context."""

    def test_format_reminder_includes_completed_count(self, tmp_state_file):
        """The reminder should mention the completed-step count so the LLM
        has continuity across a resume."""
        sm = TaskStateMachine(state_file=tmp_state_file)
        sm.start_task(task="implement fib", session_id="s1")
        sm.transition(TaskState.PLAN)
        sm.transition(TaskState.EXEC)
        sm.record_completed_step("write_file", {"path": "fib.py"}, "h1")
        sm.record_completed_step("run_tests", {"path": "fib_test.py"}, "h2")
        reminder = sm.format_reminder()
        assert "Completed: 2 steps" in reminder
        assert "implement fib" in reminder
