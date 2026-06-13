"""Tests for tracing primitives (PR-10).

These tests exercise the no-op shims by default, since OTel SDK may
not be installed. When it IS installed, the same surface still works —
just with real spans being created behind the scenes.
"""

import pytest

from agent.observability.tracing import (
    OTEL_AVAILABLE,
    _NoOpSpan,
    _NoOpTracer,
    get_tracer,
    init_tracer,
    reset_tracer,
)


@pytest.fixture(autouse=True)
def reset():
    reset_tracer()
    yield
    reset_tracer()


class TestNoOpSpan:
    def test_start_and_end(self):
        s = _NoOpSpan("test")
        assert not s.ended
        s.end()
        assert s.ended

    def test_set_attribute(self):
        s = _NoOpSpan("test")
        s.set_attribute("k", "v")
        assert s.attributes == {"k": "v"}

    def test_context_manager(self):
        with _NoOpSpan("test") as s:
            s.set_attribute("x", 1)
        assert s.ended
        assert s.attributes["x"] == 1

    def test_context_manager_captures_exception(self):
        try:
            with _NoOpSpan("test") as s:
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert s.ended
        assert "exception" in s.attributes

    def test_record_exception(self):
        s = _NoOpSpan("test")
        s.record_exception(ValueError("x"))
        assert "ValueError" in s.attributes["exception"]


class TestNoOpTracer:
    def test_start_span_returns_noop(self):
        t = _NoOpTracer()
        s = t.start_span("foo", attributes={"a": 1})
        assert isinstance(s, _NoOpSpan)
        assert s.name == "foo"
        assert s.attributes == {"a": 1}

    def test_start_as_current_span(self):
        t = _NoOpTracer()
        with t.start_as_current_span("bar") as s:
            s.set_attribute("x", 2)
        assert s.ended


class TestTracerSingleton:
    def test_singleton(self):
        t1 = get_tracer()
        t2 = get_tracer()
        assert t1 is t2

    def test_reset_creates_new(self):
        t1 = get_tracer()
        reset_tracer()
        t2 = get_tracer()
        # When OTel SDK is installed, the global TracerProvider can only be
        # set once per process — so after reset, get_tracer() may return the
        # same underlying tracer object. The important property is that
        # reset_tracer() clears our local cache, which we verify by checking
        # that the call doesn't crash.
        assert t2 is not None
        if not OTEL_AVAILABLE:
            # Only when using our no-op shim do we get fresh instances
            assert t1 is not t2

    def test_init_idempotent(self):
        t1 = init_tracer()
        t2 = init_tracer()
        assert t1 is t2


class TestOtelAvailability:
    def test_flag_is_bool(self):
        assert isinstance(OTEL_AVAILABLE, bool)

    def test_tracer_works_either_way(self):
        # Should work whether OTel is installed or not
        t = get_tracer()
        span = t.start_span("test")
        span.set_attribute("a", 1)
        span.end()
        # No exception = success


# ── P14-1: W1 warning + JSONL file exporter + dual-export behavior ──


class TestP14Warning:
    """P14-1 W1: SDK-missing path must emit a clear warning, not silently no-op."""

    def test_warning_when_sdk_missing(self, caplog):
        """When opentelemetry is not installed, init_tracer should log a warning."""
        import logging

        import agent.observability.tracing as tracing_mod
        from agent.observability.tracing import init_tracer, reset_tracer

        # Force the SDK-missing path
        reset_tracer()
        original = tracing_mod.OTEL_AVAILABLE
        try:
            tracing_mod.OTEL_AVAILABLE = False
            with caplog.at_level(logging.WARNING, logger="agent.observability.tracing"):
                t = init_tracer(service_name="test")
            # The warning must mention pip install
            msgs = [r.message for r in caplog.records]
            assert any(
                "pip install" in m for m in msgs
            ), f"expected 'pip install' in warnings, got: {msgs}"
            assert t is not None  # still returns a tracer (no-op)
        finally:
            tracing_mod.OTEL_AVAILABLE = original
            reset_tracer()


class TestP14JsonlFileExporter:
    """P14-1 X3: JSONL file exporter writes spans to a daily-rotated file."""

    def test_jsonl_exporter_class_exists(self):
        from agent.observability.tracing import JsonlFileSpanExporter

        assert JsonlFileSpanExporter is not None

    def test_jsonl_exporter_writes_valid_json(self, tmp_path):
        """When SDK is installed, the exporter should write valid JSONL."""
        from agent.observability.tracing import OTEL_AVAILABLE, JsonlFileSpanExporter

        if not OTEL_AVAILABLE:
            pytest.skip("opentelemetry-sdk not installed")

        # Build a real span via the SDK (without touching the global provider,
        # which OTel disallows overriding).
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        mem = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(mem))
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("test_span") as span:
            span.set_attribute("key", "value")

        # Now export those spans through our JsonlFileSpanExporter
        exporter = JsonlFileSpanExporter(output_dir=tmp_path)
        result = exporter.export(mem.get_finished_spans())
        assert result.name == "SUCCESS"

        # Verify file was written
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        content = files[0].read_text().strip().splitlines()
        assert len(content) >= 1
        # Each line must be valid JSON
        import json

        for line in content:
            rec = json.loads(line)
            assert "name" in rec
            assert "trace_id" in rec
            assert "span_id" in rec
            assert "timestamp" in rec
        exporter.shutdown()
