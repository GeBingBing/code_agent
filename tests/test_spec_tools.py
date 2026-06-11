"""Tests for the AC-aware spec tools (PR-06)."""

import json

import pytest

from agent.tools.spec_verifier import (
    MarkAcDoneTool,
    SpecStatusTool,
    VerifySpecACSTool,
)

SAMPLE = """# Test

## Phase 0: Setup ✅
- [x] Initialize project
- [ ] Configure CI

### P1-1: Feature A 🔜
- [ ] Task one
- [x] Task two
"""


@pytest.fixture
def workspace_with_spec(tmp_path, monkeypatch):
    """Provide a workspace with SPECS.md and patch WORKSPACE to point at it."""
    (tmp_path / "SPECS.md").write_text(SAMPLE)
    import agent.tools.spec_verifier as sv

    monkeypatch.setattr(sv, "WORKSPACE", tmp_path, raising=False)
    return tmp_path


# ── spec_status ────────────────────────────────────────────────────


class TestSpecStatusTool:
    @pytest.mark.asyncio
    async def test_returns_active_phase(self, workspace_with_spec):
        tool = SpecStatusTool()
        result = await tool.execute()
        assert result.success
        payload = json.loads(result.content)
        assert payload["active_phase"] is not None
        assert payload["progress"]["total"] >= 3

    @pytest.mark.asyncio
    async def test_unfinished_acs_listed(self, workspace_with_spec):
        tool = SpecStatusTool()
        result = await tool.execute()
        payload = json.loads(result.content)
        ids = {ac["id"] for ac in payload["unfinished_acs"]}
        # "Configure CI" (P0-2) and "Task one" (P1-1-1) should be unfinished
        assert any("Configure CI" in ac["description"] for ac in payload["unfinished_acs"])

    @pytest.mark.asyncio
    async def test_queried_phase(self, workspace_with_spec):
        tool = SpecStatusTool()
        result = await tool.execute(phase_id="P0")
        assert result.success
        payload = json.loads(result.content)
        assert payload["queried_phase"] == "P0"

    @pytest.mark.asyncio
    async def test_no_specs_returns_error(self, tmp_path, monkeypatch):
        import agent.tools.spec_verifier as sv

        monkeypatch.setattr(sv, "WORKSPACE", tmp_path, raising=False)
        tool = SpecStatusTool()
        result = await tool.execute()
        assert not result.success

    @pytest.mark.asyncio
    async def test_schema_has_optional_phase_id(self):
        tool = SpecStatusTool()
        schema = tool.schema
        assert "phase_id" in schema["function"]["parameters"]["properties"]


# ── mark_ac_done ──────────────────────────────────────────────────


class TestMarkAcDoneTool:
    @pytest.mark.asyncio
    async def test_mark_existing(self, workspace_with_spec):
        tool = MarkAcDoneTool()
        result = await tool.execute(ac_id="P0-2")
        assert result.success
        assert "P0-2" in result.content

    @pytest.mark.asyncio
    async def test_mark_nonexistent_returns_error(self, workspace_with_spec):
        tool = MarkAcDoneTool()
        result = await tool.execute(ac_id="P99-99")
        assert not result.success
        assert "not found" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_empty_ac_id_fails(self, workspace_with_spec):
        tool = MarkAcDoneTool()
        result = await tool.execute(ac_id="")
        assert not result.success

    @pytest.mark.asyncio
    async def test_persists_to_cache(self, workspace_with_spec):
        tool = MarkAcDoneTool()
        await tool.execute(ac_id="P0-2", verified_by="evaluator")
        cache = workspace_with_spec / ".spec_ac_state.json"
        assert cache.exists()
        data = json.loads(cache.read_text())
        ids = {entry["id"] for entry in data["acs"]}
        assert "P0-2" in ids

    @pytest.mark.asyncio
    async def test_verified_by_recorded(self, workspace_with_spec):
        tool = MarkAcDoneTool()
        await tool.execute(ac_id="P0-2", verified_by="human")
        cache = workspace_with_spec / ".spec_ac_state.json"
        data = json.loads(cache.read_text())
        entry = next(e for e in data["acs"] if e["id"] == "P0-2")
        assert entry["verified_by"] == "human"


# ── verify_acs ────────────────────────────────────────────────────


class TestVerifySpecACSTool:
    @pytest.mark.asyncio
    async def test_unfinished_listed(self, workspace_with_spec):
        tool = VerifySpecACSTool()
        result = await tool.execute(phase_id="P0")
        assert result.success
        payload = json.loads(result.content)
        assert payload["status"] == "incomplete"
        assert payload["phase"] == "P0"
        assert len(payload["unfinished_acs"]) >= 1

    @pytest.mark.asyncio
    async def test_no_phase_returns_error(self, tmp_path, monkeypatch):
        import agent.tools.spec_verifier as sv

        (tmp_path / "SPECS.md").write_text("# No phases here")
        monkeypatch.setattr(sv, "WORKSPACE", tmp_path, raising=False)
        tool = VerifySpecACSTool()
        result = await tool.execute()
        # No active phase, no phase_id given → error
        assert not result.success

    @pytest.mark.asyncio
    async def test_unknown_phase_returns_error(self, workspace_with_spec):
        tool = VerifySpecACSTool()
        result = await tool.execute(phase_id="P99-99")
        assert not result.success
        assert "not found" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_all_done(self, tmp_path, monkeypatch):
        """Phase where every AC is done → status 'all done'."""
        spec = """# Test
### P1: All Done ✅
- [x] One
- [x] Two
"""
        (tmp_path / "SPECS.md").write_text(spec)
        import agent.tools.spec_verifier as sv

        monkeypatch.setattr(sv, "WORKSPACE", tmp_path, raising=False)
        tool = VerifySpecACSTool()
        result = await tool.execute(phase_id="P1")
        assert result.success
        payload = json.loads(result.content)
        assert payload["status"] == "all done"
        assert payload["unfinished_acs"] == []


# ── Registration ──────────────────────────────────────────────────


class TestRegistration:
    def test_spec_status_registered(self):
        from agent.tools.base import registry

        assert registry.get("spec_status") is not None

    def test_mark_ac_done_registered(self):
        from agent.tools.base import registry

        assert registry.get("mark_ac_done") is not None

    def test_verify_acs_registered(self):
        from agent.tools.base import registry

        assert registry.get("verify_acs") is not None
