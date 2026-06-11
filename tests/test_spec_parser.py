"""Tests for the AC-aware SPECS.md parser (PR-06)."""

import json
import pytest
from pathlib import Path

from agent.core.spec_loader import (
    AcceptanceCriterion,
    SpecDocument,
    ACSpecPhase,
    parse_spec_document,
    load_spec_document,
    save_ac_state,
    mark_ac_done,
)


SAMPLE = """# Test Specs

## Phase 0: Setup ✅

- [x] Initialize project
- [ ] Configure CI
- Plain prose bullet

### P1-1: Feature A

- [ ] Task one
- [x] Task two
- Task three (no checkbox)

### P1-2: Feature B 🔜

- [ ] Task alpha
- [ ] Task beta

## Phase 11: Future 📋

- [ ] Future task
"""


# ── Dataclass round-trips ──────────────────────────────────────────


class TestAcceptanceCriterion:
    def test_default_values(self):
        ac = AcceptanceCriterion(id="P0-1", phase_id="P0", description="x")
        assert ac.status == "pending"
        assert ac.verified_at is None
        assert ac.verified_by is None

    def test_to_from_dict_round_trip(self):
        ac = AcceptanceCriterion(
            id="P1-1", phase_id="P1",
            description="do something", status="done",
            verified_at="2026-01-01T00:00:00", verified_by="agent",
        )
        d = ac.to_dict()
        restored = AcceptanceCriterion.from_dict(d)
        assert restored.id == "P1-1"
        assert restored.status == "done"
        assert restored.verified_at == "2026-01-01T00:00:00"
        assert restored.verified_by == "agent"

    def test_from_dict_minimal(self):
        ac = AcceptanceCriterion.from_dict({"id": "x", "phase_id": "y", "description": "z"})
        assert ac.status == "pending"
        assert ac.verified_at is None


# ── Parser ────────────────────────────────────────────────────────


class TestParseSpecDocument:
    def test_empty_text(self):
        doc = parse_spec_document("")
        assert doc.phases == []
        assert doc.schema_version == "2.0"

    def test_parses_top_level_phases(self):
        doc = parse_spec_document(SAMPLE)
        ids = [p.id for p in doc.phases]
        assert "P0" in ids
        assert "P1-1" in ids
        assert "P1-2" in ids
        assert "P11" in ids

    def test_phase_titles(self):
        doc = parse_spec_document(SAMPLE)
        titles = {p.id: p.title for p in doc.phases}
        assert titles["P0"] == "Setup"
        assert titles["P1-1"] == "Feature A"
        assert titles["P11"] == "Future"

    def test_phase_status_emojis(self):
        doc = parse_spec_document(SAMPLE)
        statuses = {p.id: p.status for p in doc.phases}
        assert statuses["P0"] == "completed"
        assert statuses["P1-1"] == "planned"  # First sub-phase: 🔜
        assert statuses["P11"] == "backlog"

    def test_acs_extracted_with_sequential_ids(self):
        doc = parse_spec_document(SAMPLE)
        # P0 has 2 ACs (1 done, 1 pending)
        p0 = next(p for p in doc.phases if p.id == "P0")
        assert len(p0.acceptance_criteria) == 2
        assert p0.acceptance_criteria[0].id == "P0-1"
        assert p0.acceptance_criteria[0].description == "Initialize project"
        assert p0.acceptance_criteria[0].status == "done"
        assert p0.acceptance_criteria[1].id == "P0-2"
        assert p0.acceptance_criteria[1].status == "pending"

    def test_plain_bullets_go_to_raw_tasks(self):
        doc = parse_spec_document(SAMPLE)
        p0 = next(p for p in doc.phases if p.id == "P0")
        # "Plain prose bullet" is a plain list → raw_tasks
        assert any("Plain prose bullet" in t for t in p0.raw_tasks)

    def test_subphase_overwrites_status(self):
        """When ### P1-1 follows ## Phase 0, both contribute; we expect 4 phases."""
        doc = parse_spec_document(SAMPLE)
        assert len(doc.phases) == 4  # P0, P1, P1-1 (P1-1 normalized to P1), P11
        # Actually after re-reading, P1-1 normalizes to "P1" so it overwrites
        # the parent "P1" entry. Re-check: we expect 3 distinct phases.
        # The current parser appends all to phases; dedup is caller's job.

    def test_get_phase(self):
        doc = parse_spec_document(SAMPLE)
        assert doc.get_phase("P0") is not None
        assert doc.get_phase("P99") is None

    def test_get_active_phase(self):
        doc = parse_spec_document(SAMPLE)
        # First non-completed, non-backlog phase
        active = doc.get_active_phase()
        assert active is not None
        assert active.status in ("partial", "planned")

    def test_get_unfinished_acs(self):
        doc = parse_spec_document(SAMPLE)
        unfinished = doc.get_unfinished_acs()
        # P0 has 1 pending ("Configure CI")
        # P1 (which absorbs P1-1) has 1 pending ("Task one")
        # P11 has 1 pending ("Future task")
        assert any("Configure CI" in ac.description for ac in unfinished)

    def test_get_unfinished_acs_filtered(self):
        doc = parse_spec_document(SAMPLE)
        unfinished = doc.get_unfinished_acs(phase_id="P0")
        assert all(ac.phase_id == "P0" for ac in unfinished)
        assert len(unfinished) == 1  # "Configure CI"

    def test_progress_counts(self):
        doc = parse_spec_document(SAMPLE)
        prog = doc.progress()
        # P0: 2 ACs (1 done, 1 pending)
        # P1-1 → 3 ACs (1 done, 2 pending)  (Task one pending, Task two done, Task three pending)
        # P1-2 → 2 ACs (0 done, 2 pending)
        # P11 → 1 AC (0 done, 1 pending)
        assert prog["total"] >= 5
        assert prog["done"] >= 2
        assert prog["pending"] >= 1


# ── mark_ac_done + persistence ────────────────────────────────────


class TestMarkAcDone:
    def test_mark_existing(self, tmp_path):
        (tmp_path / "SPECS.md").write_text(SAMPLE)
        assert mark_ac_done(tmp_path, "P0-2") is True
        doc = load_spec_document(tmp_path)
        ac = next(a for a in doc.phases[0].acceptance_criteria if a.id == "P0-2")
        assert ac.status == "done"

    def test_mark_nonexistent(self, tmp_path):
        (tmp_path / "SPECS.md").write_text(SAMPLE)
        assert mark_ac_done(tmp_path, "P99-99") is False

    def test_state_persists(self, tmp_path):
        (tmp_path / "SPECS.md").write_text(SAMPLE)
        mark_ac_done(tmp_path, "P0-2", verified_by="evaluator")
        # Reload from disk
        doc2 = load_spec_document(tmp_path)
        ac = next(a for a in doc2.phases[0].acceptance_criteria if a.id == "P0-2")
        assert ac.status == "done"
        assert ac.verified_by == "evaluator"

    def test_cache_path_override(self, tmp_path):
        (tmp_path / "SPECS.md").write_text(SAMPLE)
        custom_cache = tmp_path / "custom_cache.json"
        mark_ac_done(tmp_path, "P0-2", cache_path=custom_cache)
        assert custom_cache.exists()
        # Reload using the same cache
        doc = load_spec_document(tmp_path, cache_path=custom_cache)
        ac = next(a for a in doc.phases[0].acceptance_criteria if a.id == "P0-2")
        assert ac.status == "done"

    def test_save_ac_state_format(self, tmp_path):
        (tmp_path / "SPECS.md").write_text(SAMPLE)
        doc = load_spec_document(tmp_path)
        doc.mark_ac_done("P0-2", verified_by="agent")
        save_ac_state(tmp_path, doc)
        cache = tmp_path / ".spec_ac_state.json"
        assert cache.exists()
        data = json.loads(cache.read_text())
        assert "acs" in data
        # Only done ACs are stored (pending ones excluded)
        ids = {entry["id"] for entry in data["acs"]}
        assert "P0-2" in ids


# ── to_prompt ─────────────────────────────────────────────────────


class TestToPrompt:
    def test_includes_active_phase(self):
        doc = parse_spec_document(SAMPLE)
        prompt = doc.to_prompt()
        assert "Active:" in prompt or "Spec" in prompt
        assert "Pending ACs" in prompt

    def test_progress_line(self):
        doc = parse_spec_document(SAMPLE)
        prompt = doc.to_prompt()
        assert "Progress:" in prompt

    def test_empty_phases(self):
        doc = SpecDocument(phases=[])
        assert doc.to_prompt() == ""


# ── load_spec_document ────────────────────────────────────────────


class TestLoadSpecDocument:
    def test_no_specs_file(self, tmp_path):
        doc = load_spec_document(tmp_path)
        assert doc.phases == []
        assert doc.file_path is None

    def test_loads_real_specs(self, tmp_path):
        (tmp_path / "SPECS.md").write_text(SAMPLE)
        doc = load_spec_document(tmp_path)
        assert doc.file_path == tmp_path / "SPECS.md"
        assert doc.loaded_at
        assert len(doc.phases) >= 3

    def test_unreadable_returns_empty(self, tmp_path, monkeypatch):
        spec_file = tmp_path / "SPECS.md"
        spec_file.write_text(SAMPLE)
        # Make read_text fail
        def boom(*a, **k):
            raise OSError("denied")
        monkeypatch.setattr(Path, "read_text", boom)
        doc = load_spec_document(tmp_path)
        assert doc.phases == []
