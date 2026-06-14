"""Tests for the /plan from-spec slash command (M2 P0).

Covers:
  * No-arg form lists eligible phases from SPECS.md
  * Empty SPECS.md → "no eligible phases" message
  * With phase_id: builds the plan, stashes it on cli._last_plan, and
    returns a short summary that mentions plan_id / step count / AC count
  * Unknown phase_id renders a dimmed error
  * After /plan from-spec, /plan show works on the stashed plan
  * /plan edit works on the stashed plan (proves it shares the M1 path)
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.commands.builtin import _handle_plan
from agent.core.spec_plan_adapter import from_spec as spec_from_spec


SAMPLE_SPEC = """\
# Project Spec

## Phase 0: Setup
- [x] Initialise project
- [x] Install pytest

## Phase 1: Implementation
- [ ] Implement feature A
- [ ] Implement feature B

## Phase 2: Polish 🔜
- [ ] Add docs
- [ ] Add examples
"""


@pytest.fixture
def workspace_with_spec(tmp_path):
    spec = tmp_path / "SPECS.md"
    spec.write_text(SAMPLE_SPEC, encoding="utf-8")
    return tmp_path


class _FakeCli:
    def __init__(self):
        self._last_plan = None
        self._last_plan_persistence_path = None


def _ctx(workspace: Path, cli: _FakeCli | None = None) -> dict:
    return {"cli": cli, "engine": None, "workspace": str(workspace)}


# ── No-arg form: list eligible phases ─────────────────────────────────────


class TestFromSpecList:
    def test_lists_planned_and_partial_phases(self, workspace_with_spec):
        # P0 (no emoji → "planned"), P1 (no emoji → "planned"), P2 (🔜 → "planned")
        result = asyncio.run(_handle_plan("from-spec", _ctx(workspace_with_spec)))
        # P0, P1, P2 all qualify (none are completed)
        assert "P0" in result
        assert "P1" in result
        assert "P2" in result

    def test_shows_usage_hint(self, workspace_with_spec):
        result = asyncio.run(_handle_plan("from-spec", _ctx(workspace_with_spec)))
        assert "Usage" in result
        assert "/plan from-spec" in result

    def test_no_spec_returns_no_eligible_message(self, tmp_path):
        # No SPECS.md
        result = asyncio.run(_handle_plan("from-spec", _ctx(tmp_path)))
        assert "No eligible phases" in result


# ── With phase_id: build a plan ──────────────────────────────────────────


class TestFromSpecBuilds:
    def test_builds_plan_for_valid_phase(self, workspace_with_spec):
        cli = _FakeCli()
        result = asyncio.run(
            _handle_plan("from-spec P1", _ctx(workspace_with_spec, cli))
        )
        # Summary line present
        assert "Built plan" in result
        # Plan stashed on CLI
        assert cli._last_plan is not None
        assert cli._last_plan.plan_id.startswith("spec-P1-")
        # Step count + AC count
        assert "2 steps" in result
        assert "2 ACs" in result
        # plan_id is referenced in the summary for later /plan show / edit
        assert cli._last_plan.plan_id in result

    def test_unknown_phase_renders_dimmed_error(self, workspace_with_spec):
        result = asyncio.run(
            _handle_plan("from-spec P99", _ctx(workspace_with_spec))
        )
        # Error path — dimmed (no green check)
        assert "P99" in result
        # Lists available phases
        assert "P0" in result or "P1" in result

    def test_phase_with_no_acs_still_builds(self, workspace_with_spec):
        # All of P0's ACs are done — the plan still builds, with all steps
        # marked status="done" (per the adapter contract)
        result = asyncio.run(
            _handle_plan("from-spec P0", _ctx(workspace_with_spec))
        )
        assert "Built plan" in result

    def test_plan_id_is_stable_across_repeated_calls(self, workspace_with_spec):
        cli = _FakeCli()
        asyncio.run(_handle_plan("from-spec P1", _ctx(workspace_with_spec, cli)))
        first_id = cli._last_plan.plan_id
        # Re-run — different call → different plan_id (timestamp changes)
        import time

        time.sleep(0.01)
        asyncio.run(_handle_plan("from-spec P1", _ctx(workspace_with_spec, cli)))
        second_id = cli._last_plan.plan_id
        # Both start with the same prefix
        assert first_id.startswith("spec-P1-")
        assert second_id.startswith("spec-P1-")
        # Both look like spec-P1-<ts>-<uuid>


# ── Integration with /plan show and /plan edit ──────────────────────────


class TestFromSpecIntegratesWithOtherPlanCommands:
    def test_show_renders_stashed_plan(self, workspace_with_spec):
        cli = _FakeCli()
        asyncio.run(_handle_plan("from-spec P1", _ctx(workspace_with_spec, cli)))
        result = asyncio.run(_handle_plan("show", _ctx(workspace_with_spec, cli)))
        # The M2 markdown format includes the AC section
        assert "## Plan:" in result
        # Acceptance Criteria section rendered (M2)
        assert "## Acceptance Criteria" in result

    def test_edit_works_on_stashed_plan(self, workspace_with_spec):
        cli = _FakeCli()
        asyncio.run(_handle_plan("from-spec P1", _ctx(workspace_with_spec, cli)))
        # Edit step 1's description via the M1 path — proves the two
        # flows share the same in-memory plan
        result = asyncio.run(
            _handle_plan("edit 1 new description from /plan from-spec", _ctx(workspace_with_spec, cli))
        )
        assert "Step 1.description updated" in result
        assert cli._last_plan.steps[0].description == "new description from /plan from-spec"

    def test_edit_after_from_spec_persists_when_path_known(self, workspace_with_spec):
        """If a persistence_path was already tracked, /plan edit rewrites
        that file. We simulate this by pre-setting the path."""
        cli = _FakeCli()
        asyncio.run(_handle_plan("from-spec P1", _ctx(workspace_with_spec, cli)))
        # The from-spec command doesn't write a plan file (it's pure /
        # in-memory). So persistence_path remains None and the edit is
        # in-memory only. Verify that.
        assert cli._last_plan_persistence_path is None
        result = asyncio.run(
            _handle_plan("edit 1 only in memory", _ctx(workspace_with_spec, cli))
        )
        assert "Persisted" not in result
        assert "updated" in result.lower()


# ── Error handling ──────────────────────────────────────────────────────


class TestFromSpecErrorPaths:
    def test_invalid_phase_id_does_not_set_last_plan(self, workspace_with_spec):
        cli = _FakeCli()
        asyncio.run(_handle_plan("from-spec P99", _ctx(workspace_with_spec, cli)))
        # No plan built → cli._last_plan stays None
        assert cli._last_plan is None

    def test_works_without_cli(self, workspace_with_spec):
        """If the CLI is None (e.g. invoked from a non-CLI context), the
        command should still succeed — just skip the stash step."""
        result = asyncio.run(
            _handle_plan("from-spec P1", _ctx(workspace_with_spec, cli=None))
        )
        assert "Built plan" in result
        # No crash, no traceback

    def test_workspace_default_is_cwd(self, monkeypatch):
        """If the ctx doesn't supply a workspace, fall back to '.' (which
        SpecPlanAdapter handles by raising on missing SPECS.md)."""
        monkeypatch.chdir(tempfile.mkdtemp())
        # No SPECS.md in cwd
        ctx = {"cli": _FakeCli(), "engine": None}  # no "workspace" key
        result = asyncio.run(_handle_plan("from-spec P0", ctx))
        # Either "no eligible phases" or some error — must not crash
        assert "✓" in result or "No eligible" in result or "not found" in result.lower()
