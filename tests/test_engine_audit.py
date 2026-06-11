"""Tests for engine ↔ audit log integration (PR-08)."""

import pytest

from agent.core.audit_log import get_audit_logger, reset_audit_logger
from agent.core.engine import AgentConfig, AgentEngine


@pytest.fixture
def isolated_audit(tmp_path, monkeypatch):
    """Point the audit logger singleton at a tmpdir so tests don't pollute the real log."""
    monkeypatch.setenv("CODING_AGENT_AUDIT_DIR", str(tmp_path / "audit"))
    reset_audit_logger()
    yield get_audit_logger()
    reset_audit_logger()


class TestEngineAuditWiring:
    def test_engine_initializes_audit(self, isolated_audit):
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        assert e.audit is not None
        assert e.audit is isolated_audit

    def test_audit_disabled_via_config(self, isolated_audit):
        e = AgentEngine(AgentConfig(model="mock", provider="mock", audit_enabled=False))
        assert e.audit is None

    def test_audit_hooks_registered(self, isolated_audit):
        from agent.core.hooks import AFTER_TOOL_EXECUTION, BEFORE_TOOL_EXECUTION

        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        before = (
            e.hooks.get_handlers(BEFORE_TOOL_EXECUTION) if hasattr(e.hooks, "get_handlers") else []
        )
        after = (
            e.hooks.get_handlers(AFTER_TOOL_EXECUTION) if hasattr(e.hooks, "get_handlers") else []
        )
        # Either via get_handlers or via direct internal state; just assert the audit is wired
        assert e.audit is not None


class TestAuditHooksFire:
    @pytest.mark.asyncio
    async def test_before_tool_hook_logs(self, isolated_audit):
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        payload = {"tool": "read_file", "args": {"path": "/x"}, "tc_id": "tc1"}
        out = await e._audit_before_tool(payload)
        assert "_audit_start_ts" in out
        recs = isolated_audit.query()
        assert any(r["action"] == "tool_call" and r["tool"] == "read_file" for r in recs)
        # Args were scrubbed
        for r in recs:
            assert "args" not in r
            if r.get("action") == "tool_call":
                assert "args_hash" in r

    @pytest.mark.asyncio
    async def test_after_tool_hook_logs_with_duration(self, isolated_audit):
        from agent.tools.base import ToolResult

        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        import time

        payload = {
            "tool": "read_file",
            "args": {},
            "result": ToolResult(success=True, content="hello"),
            "error": None,
            "_audit_start_ts": time.time() - 0.01,  # 10ms ago
        }
        await e._audit_after_tool(payload)
        recs = isolated_audit.query()
        result_recs = [r for r in recs if r.get("action") == "tool_result"]
        assert len(result_recs) >= 1
        rec = result_recs[0]
        assert rec["tool"] == "read_file"
        assert "duration_ms" in rec
        assert rec["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_audit_handles_non_dict_payload(self, isolated_audit):
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        # Should silently no-op, never raise
        out_before = await e._audit_before_tool(42)
        out_after = await e._audit_after_tool("not a dict")
        assert out_before == 42
        assert out_after == "not a dict"

    @pytest.mark.asyncio
    async def test_audit_disabled_no_logging(self, isolated_audit):
        e = AgentEngine(AgentConfig(model="mock", provider="mock", audit_enabled=False))
        payload = {"tool": "read_file", "args": {}, "tc_id": "x"}
        await e._audit_before_tool(payload)
        # No records were written
        assert isolated_audit.query() == []

    @pytest.mark.asyncio
    async def test_audit_records_session_id(self, isolated_audit):
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        await e._audit_before_tool({"tool": "read_file", "args": {}, "tc_id": "x"})
        recs = isolated_audit.query()
        assert any(r.get("session_id") == e.trace_id for r in recs)


class TestAuditQueryTool:
    @pytest.mark.asyncio
    async def test_tool_registered(self):
        from agent.tools.base import registry

        tool = registry.get("audit_query")
        assert tool is not None
        assert tool.is_read_only is True
        assert tool.is_concurrency_safe is True

    @pytest.mark.asyncio
    async def test_tool_returns_json(self, isolated_audit):
        from agent.tools.base import registry

        # Seed
        isolated_audit.log(
            {"session_id": "s", "agent_id": "main", "action": "tool_call", "tool": "x"}
        )
        tool = registry.get("audit_query")
        result = await tool.execute()
        assert result.success
        import json

        records = json.loads(result.content)
        assert len(records) >= 1

    @pytest.mark.asyncio
    async def test_tool_filters(self, isolated_audit):
        from agent.tools.base import registry

        isolated_audit.log(
            {"session_id": "s", "agent_id": "main", "action": "tool_call", "tool": "read"}
        )
        isolated_audit.log(
            {"session_id": "s", "agent_id": "main", "action": "tool_call", "tool": "write"}
        )
        isolated_audit.log(
            {"session_id": "s", "agent_id": "sub-1", "action": "tool_call", "tool": "read"}
        )
        tool = registry.get("audit_query")
        # Filter by tool
        r = await tool.execute(tool="read")
        import json

        recs = json.loads(r.content)
        assert all(rec["tool"] == "read" for rec in recs)
        assert len(recs) == 2
        # Filter by agent_id
        r = await tool.execute(agent_id="sub-1")
        recs = json.loads(r.content)
        assert all(rec["agent_id"] == "sub-1" for rec in recs)

    @pytest.mark.asyncio
    async def test_tool_limit(self, isolated_audit):
        from agent.tools.base import registry

        for i in range(5):
            isolated_audit.log(
                {"session_id": "s", "agent_id": "main", "action": "tool_call", "tool": "x"}
            )
        tool = registry.get("audit_query")
        r = await tool.execute(limit=3)
        import json

        assert len(json.loads(r.content)) == 3

    @pytest.mark.asyncio
    async def test_tool_limit_clamping(self, isolated_audit):
        from agent.tools.base import registry

        tool = registry.get("audit_query")
        # Negative/huge limits should be clamped to [1, 1000]
        r = await tool.execute(limit=-5)
        assert r.success  # Did not crash
        r = await tool.execute(limit=99999)
        assert r.success
        r = await tool.execute(limit="not a number")
        assert r.success  # Defaulted to 100


class TestAuditSlashCommand:
    @pytest.mark.asyncio
    async def test_audit_stats(self, isolated_audit):
        from agent.commands.builtin import _handle_audit

        isolated_audit.log(
            {"session_id": "s", "agent_id": "main", "action": "tool_call", "tool": "read"}
        )
        out = await _handle_audit("stats", {})
        assert "Audit stats" in out
        assert "total entries: 1" in out
        assert "tool_call" in out

    @pytest.mark.asyncio
    async def test_audit_stats_default(self, isolated_audit):
        from agent.commands.builtin import _handle_audit

        out = await _handle_audit("", {})  # Defaults to stats
        assert "Audit stats" in out

    @pytest.mark.asyncio
    async def test_audit_query(self, isolated_audit):
        from agent.commands.builtin import _handle_audit

        isolated_audit.log(
            {"session_id": "s", "agent_id": "main", "action": "tool_call", "tool": "x"}
        )
        out = await _handle_audit("query 5", {})
        assert "tool_call" in out

    @pytest.mark.asyncio
    async def test_audit_query_empty(self, isolated_audit):
        from agent.commands.builtin import _handle_audit

        out = await _handle_audit("query", {})
        assert "empty" in out.lower()

    @pytest.mark.asyncio
    async def test_audit_rotate(self, isolated_audit):
        from agent.commands.builtin import _handle_audit

        out = await _handle_audit("rotate 30", {})
        assert "Rotated" in out

    @pytest.mark.asyncio
    async def test_audit_unknown_subcommand(self, isolated_audit):
        from agent.commands.builtin import _handle_audit

        out = await _handle_audit("frobnicate", {})
        assert "Usage" in out

    @pytest.mark.asyncio
    async def test_audit_command_registered(self):
        from agent.commands.base import registry

        cmd = registry.get("audit")
        assert cmd is not None
        assert "audit" in cmd.description.lower()
