"""Tests for spec_loader and spec_verifier tools."""

import pytest
from pathlib import Path
from agent.core.spec_loader import (
    load_spec,
    _parse_spec,
    _parse_tasks,
    mark_task_done,
    verify_against_spec,
    SpecTask,
)


SAMPLE_SPECS = """# Test Spec

## Phase 0: Setup ✅

- [x] Initialize project
- [ ] Configure CI

### P1-1: Feature A

- [ ] Task one
- [x] Task two
- Plain task

### P1-2: Feature B 🔜

- [ ] Task three
- [ ] Task four

## Phase 11: Future 📋

- [ ] Future task
"""


class TestSpecLoader:
    def test_parse_spec_with_tasks(self, tmp_path: Path):
        spec_file = tmp_path / "SPECS.md"
        spec_file.write_text(SAMPLE_SPECS, encoding="utf-8")

        ctx = load_spec(tmp_path)
        assert ctx.source_path == str(spec_file)

        # Should parse Phase 0, P1-1, P1-2, and Phase 11
        assert len(ctx.phases) == 4

        p0 = ctx.phases[0]
        assert p0.number == 0
        assert p0.name == "Setup"
        assert p0.status == "completed"
        assert len(p0.tasks) == 2
        assert p0.tasks[0].description == "Initialize project"
        assert p0.tasks[0].done is True
        assert p0.tasks[1].description == "Configure CI"
        assert p0.tasks[1].done is False

        p1 = ctx.phases[1]
        assert p1.number == 1
        assert p1.name == "Feature A"
        assert p1.status == "planned"
        assert len(p1.tasks) == 3
        assert p1.tasks[0].description == "Task one"
        assert p1.tasks[0].done is False
        assert p1.tasks[1].description == "Task two"
        assert p1.tasks[1].done is True
        assert p1.tasks[2].description == "Plain task"
        assert p1.tasks[2].done is False

        p2 = ctx.phases[2]
        assert p2.number == 1  # P1-2 -> main phase 1
        assert p2.name == "Feature B"
        assert p2.status == "planned"
        assert len(p2.tasks) == 2
        assert all(not t.done for t in p2.tasks)

    def test_active_phase(self, tmp_path: Path):
        spec_file = tmp_path / "SPECS.md"
        spec_file.write_text(SAMPLE_SPECS, encoding="utf-8")

        ctx = load_spec(tmp_path)
        # P1-1 is first planned phase
        assert ctx.active_phase is not None
        assert ctx.active_phase.number == 1

    def test_to_prompt(self, tmp_path: Path):
        spec_file = tmp_path / "SPECS.md"
        spec_file.write_text(SAMPLE_SPECS, encoding="utf-8")

        ctx = load_spec(tmp_path)
        prompt = ctx.to_prompt()
        assert "Current phase" in prompt
        assert "Feature A" in prompt
        assert "Pending tasks" in prompt
        assert "Task one" in prompt
        assert "Completed tasks" in prompt
        assert "Task two" in prompt

    def test_no_specs_file(self, tmp_path: Path):
        ctx = load_spec(tmp_path)
        assert ctx.phases == []
        assert ctx.active_phase is None

    def test_get_phase(self, tmp_path: Path):
        spec_file = tmp_path / "SPECS.md"
        spec_file.write_text(SAMPLE_SPECS, encoding="utf-8")

        ctx = load_spec(tmp_path)
        assert ctx.get_phase(1) is not None
        assert ctx.get_phase(1).name == "Feature A"
        assert ctx.get_phase(0) is not None
        assert ctx.get_phase(0).name == "Setup"
        assert ctx.get_phase(99) is None

    def test_all_pending_tasks(self, tmp_path: Path):
        spec_file = tmp_path / "SPECS.md"
        spec_file.write_text(SAMPLE_SPECS, encoding="utf-8")

        ctx = load_spec(tmp_path)
        pending = ctx.all_pending_tasks()
        assert 1 in pending
        assert len(pending[1]) == 2  # Task one and Plain task


class TestMarkTaskDone:
    def test_mark_task_done(self, tmp_path: Path):
        spec_file = tmp_path / "SPECS.md"
        spec_file.write_text(SAMPLE_SPECS, encoding="utf-8")

        success = mark_task_done(tmp_path, 1, "Task one")
        assert success is True

        content = spec_file.read_text(encoding="utf-8")
        assert "- [x] Task one" in content
        assert "- [x] Task two" in content  # Already done, unchanged

    def test_mark_nonexistent_task(self, tmp_path: Path):
        spec_file = tmp_path / "SPECS.md"
        spec_file.write_text(SAMPLE_SPECS, encoding="utf-8")

        success = mark_task_done(tmp_path, 1, "Nonexistent task")
        assert success is False

    def test_mark_task_done_no_specs(self, tmp_path: Path):
        success = mark_task_done(tmp_path, 1, "Task one")
        assert success is False


class TestVerifyAgainstSpec:
    def test_verify(self, tmp_path: Path):
        spec_file = tmp_path / "SPECS.md"
        spec_file.write_text(SAMPLE_SPECS, encoding="utf-8")

        report = verify_against_spec(tmp_path, "Implemented task two")
        assert "error" not in report
        assert report["coverage"] > 0
        assert len(report["completed_tasks"]) == 2  # Phase 0 init + Phase 1 task two
        # "Task" keyword in summary matches many pending tasks heuristically
        assert len(report["pending_tasks"]) >= 1

    def test_verify_no_specs(self, tmp_path: Path):
        report = verify_against_spec(tmp_path, "summary")
        assert "error" in report
        assert report["coverage"] == 0.0


class TestParseTasks:
    def test_checklist_tasks(self):
        section = """
- [x] Done task
- [ ] Pending task
- Plain task
"""
        tasks = _parse_tasks(section)
        assert len(tasks) == 3
        assert tasks[0].done is True
        assert tasks[0].description == "Done task"
        assert tasks[1].done is False
        assert tasks[1].description == "Pending task"
        assert tasks[2].done is False
        assert tasks[2].description == "Plain task"

    def test_stops_at_subheading(self):
        section = """
- [ ] Task one

## Next Section
- [ ] Should not appear
"""
        tasks = _parse_tasks(section)
        assert len(tasks) == 1
        assert tasks[0].description == "Task one"
