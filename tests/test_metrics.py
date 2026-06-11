"""Tests for metrics primitives (PR-10)."""

import pytest

from agent.observability.metrics import (
    AgentMetrics,
    _NoOpCounter,
    _NoOpHistogram,
    _NoOpMeter,
    get_metrics,
    reset_meter,
)


@pytest.fixture(autouse=True)
def reset():
    reset_meter()
    yield
    reset_meter()


class TestNoOpCounter:
    def test_add_increments_total(self):
        c = _NoOpCounter("c")
        c.add(1)
        c.add(2)
        assert c.value() == 3

    def test_add_with_attributes(self):
        c = _NoOpCounter("c")
        c.add(1, {"tool": "read"})
        c.add(1, {"tool": "read"})
        c.add(1, {"tool": "write"})
        assert c.value_for(tool="read") == 2
        assert c.value_for(tool="write") == 1
        assert c.value() == 3

    def test_default_value(self):
        c = _NoOpCounter("c")
        assert c.value() == 0
        assert c.value_for(tool="missing") == 0


class TestNoOpHistogram:
    def test_record_adds_sample(self):
        h = _NoOpHistogram("h")
        h.record(10)
        h.record(20)
        assert h.count() == 2

    def test_mean_calculated(self):
        h = _NoOpHistogram("h")
        h.record(10)
        h.record(20)
        h.record(30)
        assert h.mean() == 20

    def test_mean_empty(self):
        h = _NoOpHistogram("h")
        assert h.mean() == 0

    def test_samples_preserved(self):
        h = _NoOpHistogram("h")
        h.record(5, {"tool": "x"})
        samples = h.samples()
        assert len(samples) == 1
        assert samples[0][0] == 5


class TestNoOpMeter:
    def test_create_counter(self):
        m = _NoOpMeter()
        c = m.create_counter("a")
        assert isinstance(c, _NoOpCounter)

    def test_counter_idempotent(self):
        m = _NoOpMeter()
        c1 = m.create_counter("a")
        c2 = m.create_counter("a")
        assert c1 is c2

    def test_create_histogram(self):
        m = _NoOpMeter()
        h = m.create_histogram("a")
        assert isinstance(h, _NoOpHistogram)

    def test_up_down_counter(self):
        m = _NoOpMeter()
        c = m.create_up_down_counter("a")
        # Up-down counters are counters in our shim
        c.add(5)
        c.add(-3)
        # Note: our shim doesn't model "down" semantics — just adds.
        # This is acceptable for our use case (no decrement happens).
        assert c.value() == 2


class TestAgentMetrics:
    def test_init_creates_four_instruments(self):
        m = AgentMetrics(meter=_NoOpMeter())
        assert m.tool_call_counter is not None
        assert m.tool_duration is not None
        assert m.tool_failure_counter is not None
        assert m.token_usage_counter is not None

    def test_record_tool_call_success(self):
        m = AgentMetrics(meter=_NoOpMeter())
        m.record_tool_call("read", 12.5, success=True)
        assert m.tool_call_counter.value() == 1
        assert m.tool_call_counter.value_for(tool="read", status="ok") == 1
        assert m.tool_failure_counter.value() == 0
        assert m.tool_duration.count() == 1

    def test_record_tool_call_failure(self):
        m = AgentMetrics(meter=_NoOpMeter())
        m.record_tool_call("write", 100, success=False)
        assert m.tool_call_counter.value_for(tool="write", status="fail") == 1
        assert m.tool_failure_counter.value_for(tool="write") == 1

    def test_negative_duration_clamped(self):
        m = AgentMetrics(meter=_NoOpMeter())
        m.record_tool_call("x", -5, True)
        # Clamped to 0
        assert m.tool_duration.samples()[0][0] == 0.0

    def test_record_tokens_split_input_output(self):
        m = AgentMetrics(meter=_NoOpMeter())
        m.record_tokens(100, 50, "gpt-4o")
        assert m.token_usage_counter.value_for(type="input", model="gpt-4o") == 100
        assert m.token_usage_counter.value_for(type="output", model="gpt-4o") == 50

    def test_record_tokens_skips_zero(self):
        m = AgentMetrics(meter=_NoOpMeter())
        m.record_tokens(0, 0, "x")
        assert m.token_usage_counter.value() == 0


class TestMetricsSingleton:
    def test_singleton(self):
        m1 = get_metrics()
        m2 = get_metrics()
        assert m1 is m2

    def test_reset_creates_new(self):
        m1 = get_metrics()
        reset_meter()
        m2 = get_metrics()
        assert m1 is not m2
