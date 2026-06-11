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
