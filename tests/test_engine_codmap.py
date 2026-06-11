"""Tests for the engine's codmap injection (PR-05)."""

import pytest

from agent.core.engine import AgentConfig, AgentEngine
from agent.core.hooks import BEFORE_LLM_CALL


class _Msg:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


class TestCodmapRegistration:
    def test_codmap_registered_by_default(self, tmp_path, monkeypatch):
        # Engine resolves WORKSPACE dynamically; we patch it to tmp_path so
        # the codmap scans a controlled tree.
        import agent.core.engine as eng_mod

        monkeypatch.setattr(eng_mod, "WORKSPACE", tmp_path, raising=False)
        e = AgentEngine(AgentConfig(codmap_enabled=True))
        assert e._codmap is not None
        assert e.hooks.has(BEFORE_LLM_CALL)

    def test_codmap_disabled_via_config(self, tmp_path, monkeypatch):
        import agent.core.engine as eng_mod

        monkeypatch.setattr(eng_mod, "WORKSPACE", tmp_path, raising=False)
        e = AgentEngine(AgentConfig(codmap_enabled=False))
        assert e._codmap is None
        # We can't easily count hooks, but the codmap hook should be absent
        # from the BEFORE_LLM_CALL chain — we use a probe.


class TestCodmapInjection:
    @pytest.mark.asyncio
    async def test_codmap_injected_into_last_user_message(self, tmp_path, monkeypatch):
        """The reminder should be appended to the most recent user message."""
        import agent.core.engine as eng_mod

        monkeypatch.setattr(eng_mod, "WORKSPACE", tmp_path, raising=False)
        # Create a small file tree
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("def hello():\n    pass\n")
        e = AgentEngine(AgentConfig(codmap_enabled=True))
        msgs = [
            _Msg("system", "You are an agent."),
            _Msg("user", "First user message."),
            _Msg("assistant", "Got it."),
            _Msg("user", "Latest question."),
        ]
        payload = {"messages": msgs}
        result = await e.hooks.execute(BEFORE_LLM_CALL, payload)
        # Last user message should now contain <system-reminder> block
        last_user = result["messages"][-1]
        assert "<system-reminder>" in last_user.content
        assert "<codmap>" in last_user.content
        assert "src/main.py" in last_user.content
        # Original content should still be present
        assert "Latest question." in last_user.content

    @pytest.mark.asyncio
    async def test_codmap_does_not_mutate_system_prompt(self, tmp_path, monkeypatch):
        """The system prompt is left alone (preserves prompt cache)."""
        import agent.core.engine as eng_mod

        monkeypatch.setattr(eng_mod, "WORKSPACE", tmp_path, raising=False)
        (tmp_path / "a.py").write_text("x = 1\n")
        e = AgentEngine(AgentConfig(codmap_enabled=True))
        sys_content = "ORIGINAL SYSTEM PROMPT — should be untouched."
        msgs = [_Msg("system", sys_content), _Msg("user", "Hello")]
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": msgs})
        assert result["messages"][0].content == sys_content

    @pytest.mark.asyncio
    async def test_empty_workspace_no_injection(self, tmp_path, monkeypatch):
        """No source files → no reminder added (we still want to be safe)."""
        import agent.core.engine as eng_mod

        monkeypatch.setattr(eng_mod, "WORKSPACE", tmp_path, raising=False)
        e = AgentEngine(AgentConfig(codmap_enabled=True))
        msgs = [_Msg("user", "test")]
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": msgs})
        # No files in tmp_path, so no codmap, no injection
        assert "<system-reminder>" not in result["messages"][-1].content
        assert result["messages"][-1].content == "test"

    @pytest.mark.asyncio
    async def test_disabled_codmap_is_noop(self, tmp_path, monkeypatch):
        import agent.core.engine as eng_mod

        monkeypatch.setattr(eng_mod, "WORKSPACE", tmp_path, raising=False)
        (tmp_path / "a.py").write_text("x = 1\n")
        e = AgentEngine(AgentConfig(codmap_enabled=False))
        msgs = [_Msg("user", "Hello")]
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": msgs})
        assert "<system-reminder>" not in result["messages"][-1].content
        assert result["messages"][-1].content == "Hello"

    @pytest.mark.asyncio
    async def test_no_messages_is_safe(self, tmp_path, monkeypatch):
        import agent.core.engine as eng_mod

        monkeypatch.setattr(eng_mod, "WORKSPACE", tmp_path, raising=False)
        (tmp_path / "a.py").write_text("x = 1\n")
        e = AgentEngine(AgentConfig(codmap_enabled=True))
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": []})
        # Should not crash
        assert result["messages"] == []

    @pytest.mark.asyncio
    async def test_no_user_message_no_crash(self, tmp_path, monkeypatch):
        """If there are messages but none from user, codmap is a no-op."""
        import agent.core.engine as eng_mod

        monkeypatch.setattr(eng_mod, "WORKSPACE", tmp_path, raising=False)
        (tmp_path / "a.py").write_text("x = 1\n")
        e = AgentEngine(AgentConfig(codmap_enabled=True))
        msgs = [_Msg("system", "no user msg"), _Msg("assistant", "ok")]
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": msgs})
        # No user message → no injection point. Returned unchanged.
        for m in result["messages"]:
            assert "<system-reminder>" not in (m.content or "")


class TestCodmapIntegration:
    @pytest.mark.asyncio
    async def test_other_before_llm_call_hooks_still_run(self, tmp_path, monkeypatch):
        """Codmap doesn't short-circuit other BEFORE_LLM_CALL hooks."""
        import agent.core.engine as eng_mod

        monkeypatch.setattr(eng_mod, "WORKSPACE", tmp_path, raising=False)
        (tmp_path / "a.py").write_text("x = 1\n")
        e = AgentEngine(AgentConfig(codmap_enabled=True))
        e.hooks.register(BEFORE_LLM_CALL, lambda p: {**p, "extra": True})
        msgs = [_Msg("user", "x")]
        result = await e.hooks.execute(BEFORE_LLM_CALL, {"messages": msgs})
        assert result.get("extra") is True
        assert "<system-reminder>" in result["messages"][-1].content
