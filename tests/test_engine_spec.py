"""Tests for the engine's spec/AC injection (PR-06)."""

import pytest

from agent.core.engine import AgentConfig, AgentEngine
from agent.core.hooks import BEFORE_LLM_CALL


class _Msg:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


SAMPLE = """# Test Specs

## Phase 0: Setup ✅
- [x] Initialize project
- [ ] Configure CI

### P1-1: Feature A 🔜
- [ ] Implement feature
- [ ] Write tests
"""


@pytest.fixture
def workspace_with_spec(tmp_path, monkeypatch):
    (tmp_path / "SPECS.md").write_text(SAMPLE)
    import agent.core.engine as eng

    monkeypatch.setattr(eng, "WORKSPACE", tmp_path, raising=False)
    return tmp_path


class TestEngineSpecDocument:
    def test_spec_document_loaded(self, workspace_with_spec):
        e = AgentEngine(AgentConfig(codmap_enabled=False, spec_ac_inject=True))
        assert e.spec_document is not None
        assert len(e.spec_document.phases) >= 2

    def test_no_spec_returns_none(self, tmp_path, monkeypatch):
        import agent.core.engine as eng

        monkeypatch.setattr(eng, "WORKSPACE", tmp_path, raising=False)
        e = AgentEngine(AgentConfig(codmap_enabled=False, spec_ac_inject=True))
        assert e.spec_document is not None  # Empty doc, not None
        assert e.spec_document.phases == []


class TestSpecAcInjection:
    @pytest.mark.asyncio
    async def test_pending_acs_injected(self, workspace_with_spec):
        e = AgentEngine(AgentConfig(codmap_enabled=False, spec_ac_inject=True))
        msgs = [_Msg("user", "Do the thing")]
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": msgs})
        last = result["messages"][-1]
        assert "<spec_acs>" in last.content
        assert "Configure CI" in last.content or "Implement feature" in last.content

    @pytest.mark.asyncio
    async def test_active_phase_included(self, workspace_with_spec):
        e = AgentEngine(AgentConfig(codmap_enabled=False, spec_ac_inject=True))
        msgs = [_Msg("user", "Hi")]
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": msgs})
        last = result["messages"][-1]
        assert "Active phase:" in last.content
        assert "P1-1" in last.content

    @pytest.mark.asyncio
    async def test_no_user_message_is_safe(self, workspace_with_spec):
        e = AgentEngine(AgentConfig(codmap_enabled=False, spec_ac_inject=True))
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": [_Msg("system", "x")]})
        # No user message → no injection point
        for m in result["messages"]:
            assert "<spec_acs>" not in (m.content or "")

    @pytest.mark.asyncio
    async def test_completed_phase_no_inject(self, tmp_path, monkeypatch):
        spec = """# Test
## Phase 0: All Done ✅
- [x] Done
"""
        (tmp_path / "SPECS.md").write_text(spec)
        import agent.core.engine as eng

        monkeypatch.setattr(eng, "WORKSPACE", tmp_path, raising=False)
        e = AgentEngine(AgentConfig(codmap_enabled=False, spec_ac_inject=True))
        msgs = [_Msg("user", "Hi")]
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": msgs})
        # No pending ACs, no active phase → no injection
        assert "<spec_acs>" not in result["messages"][-1].content

    @pytest.mark.asyncio
    async def test_disabled_via_config(self, workspace_with_spec):
        e = AgentEngine(AgentConfig(codmap_enabled=False, spec_ac_inject=False))
        msgs = [_Msg("user", "Hi")]
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": msgs})
        # spec_ac_inject is off, so no spec_acs reminder
        # (codmap is also off)
        assert "<spec_acs>" not in result["messages"][-1].content
        assert "<codmap>" not in result["messages"][-1].content

    @pytest.mark.asyncio
    async def test_no_specs_file(self, tmp_path, monkeypatch):
        import agent.core.engine as eng

        monkeypatch.setattr(eng, "WORKSPACE", tmp_path, raising=False)
        e = AgentEngine(AgentConfig(codmap_enabled=False, spec_ac_inject=True))
        msgs = [_Msg("user", "Hi")]
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": msgs})
        # No SPECS.md → no spec_acs reminder
        assert "<spec_acs>" not in result["messages"][-1].content

    @pytest.mark.asyncio
    async def test_invalid_payload_is_noop(self, workspace_with_spec):
        e = AgentEngine(AgentConfig(codmap_enabled=False, spec_ac_inject=True))
        # Pass a non-dict payload — should be ignored, not crash
        result = await e.hooks.execute(BEFORE_LLM_CALL, payload="garbage")
        # Returns the payload unchanged
        assert result == "garbage"
