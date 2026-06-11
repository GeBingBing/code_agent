"""Tests for engine ↔ OpenTelemetry integration (PR-10)."""

import asyncio
import pytest

from agent.core.engine import AgentEngine, AgentConfig
from agent.observability.tracing import reset_tracer
from agent.observability.metrics import reset_meter, get_metrics


@pytest.fixture(autouse=True)
def reset_otel():
    reset_tracer()
    reset_meter()
    yield
    reset_tracer()
    reset_meter()


class TestEngineInitializesOTel:
    def test_default_engine_has_tracer_and_metrics(self):
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        assert e.tracer is not None
        assert e.metrics is not None

    def test_disabled_via_config(self):
        e = AgentEngine(AgentConfig(model="mock", provider="mock", otel_enabled=False))
        assert e.tracer is None
        assert e.metrics is None


class TestOTelHooksFire:
    @pytest.mark.asyncio
    async def test_before_tool_creates_span(self):
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        payload = {"tool": "read_file", "args": {}, "tc_id": "x"}
        out = await e._otel_before_tool(payload)
        span = out.get("_otel_span")
        assert span is not None
        # Both real and no-op spans support name/attribute set
        if hasattr(span, "name"):
            assert span.name == "tool.execute"

    @pytest.mark.asyncio
    async def test_after_tool_ends_span_and_records_metrics(self):
        from agent.tools.base import ToolResult
        import time
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        # Set up — simulate that before-hook ran
        before = {"tool": "read_file", "args": {}, "tc_id": "x"}
        await e._otel_before_tool(before)
        after = dict(before)
        after["result"] = ToolResult(success=True, content="ok")
        after["error"] = None
        # Force a small elapsed time
        after["_otel_start_ts"] = time.time() - 0.01
        await e._otel_after_tool(after)
        # Span should be ended (no-op exposes .ended; real has is_recording())
        span = after["_otel_span"]
        if hasattr(span, "ended"):
            assert span.ended
        # Metrics should reflect the call
        m = get_metrics()
        # When using real OTel, our no-op shim isn't in use. Skip strict count
        # check in that case — just assert no exception was raised.
        if hasattr(m.tool_call_counter, "value_for"):
            assert m.tool_call_counter.value_for(tool="read_file", status="ok") >= 1

    @pytest.mark.asyncio
    async def test_after_tool_records_failure(self):
        from agent.tools.base import ToolResult
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        payload = {
            "tool": "write_file",
            "result": ToolResult(success=False, content="", error="denied"),
            "error": "denied",
            "_otel_span": e.tracer.start_span("tool.execute"),
        }
        await e._otel_after_tool(payload)
        m = get_metrics()
        if hasattr(m.tool_failure_counter, "value_for"):
            assert m.tool_failure_counter.value_for(tool="write_file") >= 1

    @pytest.mark.asyncio
    async def test_before_llm_creates_span(self):
        from agent.llm.client import Message
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        payload = {"messages": [Message(role="user", content="hi")]}
        out = await e._otel_before_llm(payload)
        span = out.get("_otel_llm_span")
        assert span is not None
        # no-op exposes attributes dict
        if hasattr(span, "attributes"):
            assert span.attributes.get("llm.message_count") == 1

    @pytest.mark.asyncio
    async def test_after_llm_records_token_usage(self):
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        payload = {"messages": [], "usage": {"input_tokens": 50, "output_tokens": 30}}
        await e._otel_before_llm(payload)
        await e._otel_after_llm(payload)
        m = get_metrics()
        if hasattr(m.token_usage_counter, "value_for"):
            assert m.token_usage_counter.value_for(type="input", model="mock") >= 50
            assert m.token_usage_counter.value_for(type="output", model="mock") >= 30

    @pytest.mark.asyncio
    async def test_handles_non_dict_payload(self):
        e = AgentEngine(AgentConfig(model="mock", provider="mock"))
        # Engine hooks may receive ints from buggy callers; must not crash
        assert await e._otel_before_tool(42) == 42
        assert await e._otel_after_tool("not a dict") == "not a dict"
        assert await e._otel_before_llm(None) is None
        assert await e._otel_after_llm(None) is None

    @pytest.mark.asyncio
    async def test_disabled_engine_skips_otel(self):
        e = AgentEngine(AgentConfig(model="mock", provider="mock", otel_enabled=False))
        payload = {"tool": "x", "args": {}}
        out = await e._otel_before_tool(payload)
        # No span added when tracer is None
        assert "_otel_span" not in out


class TestDiagnosticsTools:
    @pytest.mark.asyncio
    async def test_metrics_query_tool_registered(self):
        from agent.tools.base import registry
        t = registry.get("metrics_query")
        assert t is not None
        assert t.is_read_only is True

    @pytest.mark.asyncio
    async def test_logs_query_tool_registered(self):
        from agent.tools.base import registry
        t = registry.get("logs_query")
        assert t is not None
        assert t.is_read_only is True

    @pytest.mark.asyncio
    async def test_metrics_query_returns_json(self):
        from agent.tools.base import registry
        import json
        # Seed metrics
        m = get_metrics()
        m.record_tool_call("read", 10, True)
        m.record_tool_call("read", 20, True)
        m.record_tool_call("write", 100, False)
        tool = registry.get("metrics_query")
        r = await tool.execute()
        assert r.success
        data = json.loads(r.content)
        # When using no-op shim (no OTel), should contain our 4 core metrics.
        # When using real OTel, metrics flow to collector — note key present.
        if "note" not in data:
            assert "agent_tool_call_total" in data
            assert data["agent_tool_call_total"]["total"] >= 3

    @pytest.mark.asyncio
    async def test_metrics_query_filter(self):
        from agent.tools.base import registry
        import json
        m = get_metrics()
        m.record_tool_call("read", 10, True)
        tool = registry.get("metrics_query")
        r = await tool.execute(metric="duration")
        assert r.success
        data = json.loads(r.content)
        # All keys must contain "duration" (or be note in real-otel mode)
        for k in data.keys():
            assert "duration" in k or k == "note"

    @pytest.mark.asyncio
    async def test_logs_query_missing_file(self, tmp_path):
        from agent.tools.base import registry
        tool = registry.get("logs_query")
        r = await tool.execute(path=str(tmp_path / "no.log"))
        assert r.success
        assert r.metadata["count"] == 0

    @pytest.mark.asyncio
    async def test_logs_query_reads_file(self, tmp_path):
        from agent.tools.base import registry
        log = tmp_path / "agent.log"
        lines = [
            '{"level":"INFO","message":"a"}',
            '{"level":"ERROR","message":"b"}',
            '{"level":"INFO","message":"c"}',
        ]
        log.write_text("\n".join(lines) + "\n")
        tool = registry.get("logs_query")
        r = await tool.execute(path=str(log), limit=10)
        assert r.success
        assert r.metadata["count"] == 3

    @pytest.mark.asyncio
    async def test_logs_query_level_filter(self, tmp_path):
        from agent.tools.base import registry
        log = tmp_path / "agent.log"
        lines = [
            '{"level":"INFO","message":"a"}',
            '{"level":"ERROR","message":"b"}',
            '{"level":"INFO","message":"c"}',
        ]
        log.write_text("\n".join(lines) + "\n")
        tool = registry.get("logs_query")
        r = await tool.execute(path=str(log), level="ERROR")
        assert r.success
        assert r.metadata["count"] == 1


class TestJSONFormatter:
    def test_basic_record(self):
        import logging
        from agent.observability.logging import JSONFormatter
        import json

        f = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="x.py", lineno=10,
            msg="hello", args=(), exc_info=None, func="f",
        )
        line = f.format(record)
        parsed = json.loads(line)
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "hello"
        assert parsed["logger"] == "test"
        assert parsed["line"] == 10

    def test_handles_extras(self):
        import logging
        from agent.observability.logging import JSONFormatter
        import json

        f = JSONFormatter()
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname="x", lineno=1,
            msg="m", args=(), exc_info=None,
        )
        record.tool = "read_file"
        record.duration_ms = 12.3
        parsed = json.loads(f.format(record))
        assert parsed["tool"] == "read_file"
        assert parsed["duration_ms"] == 12.3

    def test_handles_exception(self):
        import logging
        import sys
        from agent.observability.logging import JSONFormatter
        import json

        f = JSONFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname="x", lineno=1,
            msg="m", args=(), exc_info=exc_info,
        )
        parsed = json.loads(f.format(record))
        assert "ValueError" in parsed["exception"]
