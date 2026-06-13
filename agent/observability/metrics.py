"""Metrics primitives (PR-10).

Mirrors the tracing module's optional-OTel pattern. Provides four
core counters/histograms used by the engine:
- agent_tool_call_total (counter, labels: tool, status)
- agent_tool_duration_ms (histogram, labels: tool)
- agent_tool_failure_total (counter, labels: tool)
- agent_token_usage_total (counter, labels: type, model)

When OTel is missing, these become local in-memory counters that
still expose `value()` / `samples()` for in-process inspection.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


try:
    from opentelemetry import metrics as _otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    OTEL_METRICS_AVAILABLE = True
except ImportError:
    OTEL_METRICS_AVAILABLE = False
    _otel_metrics = None  # type: ignore


# ── No-op shims ───────────────────────────────────────────────────


class _NoOpCounter:
    """A local counter that records add() calls — sufficient for tests."""

    def __init__(self, name: str):
        self.name = name
        self._total: float = 0.0
        self._by_attrs: Dict[Tuple, float] = {}

    def add(self, value: float, attributes: Optional[Dict[str, Any]] = None) -> None:
        self._total += value
        key = tuple(sorted((attributes or {}).items()))
        self._by_attrs[key] = self._by_attrs.get(key, 0.0) + value

    def value(self) -> float:
        return self._total

    def value_for(self, **attributes) -> float:
        key = tuple(sorted(attributes.items()))
        return self._by_attrs.get(key, 0.0)


class _NoOpHistogram:
    def __init__(self, name: str):
        self.name = name
        self._samples: List[Tuple[float, Tuple]] = []

    def record(self, value: float, attributes: Optional[Dict[str, Any]] = None) -> None:
        key = tuple(sorted((attributes or {}).items()))
        self._samples.append((value, key))

    def samples(self) -> List[Tuple[float, Tuple]]:
        return list(self._samples)

    def count(self) -> int:
        return len(self._samples)

    def mean(self) -> float:
        if not self._samples:
            return 0.0
        return sum(v for v, _ in self._samples) / len(self._samples)


class _NoOpMeter:
    """Drop-in for otel.Meter."""

    def __init__(self):
        self._counters: Dict[str, _NoOpCounter] = {}
        self._histograms: Dict[str, _NoOpHistogram] = {}

    def create_counter(self, name: str, **_) -> _NoOpCounter:
        if name not in self._counters:
            self._counters[name] = _NoOpCounter(name)
        return self._counters[name]

    def create_histogram(self, name: str, **_) -> _NoOpHistogram:
        if name not in self._histograms:
            self._histograms[name] = _NoOpHistogram(name)
        return self._histograms[name]

    def create_up_down_counter(self, name: str, **_) -> _NoOpCounter:
        # Same shape as counter for our purposes
        return self.create_counter(name)


# ── Global meter ─────────────────────────────────────────────────


_meter: Optional[Any] = None


def init_meter(otlp_endpoint: Optional[str] = None) -> Any:
    global _meter
    if _meter is not None:
        return _meter
    if not OTEL_METRICS_AVAILABLE:
        # P14-1 W1: explicit warning, no longer silent no-op
        logger.warning(
            "opentelemetry-sdk is not installed; metrics collection is disabled. "
            "Install with `pip install -e .[observability]` to enable counter/histogram export."
        )
        _meter = _NoOpMeter()
        return _meter
    try:
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                    OTLPMetricExporter,
                )

                exporter = OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True)
                reader = PeriodicExportingMetricReader(exporter)
                provider = MeterProvider(metric_readers=[reader])
            except ImportError:
                logger.warning("OTLP metric exporter not installed; using bare MeterProvider")
                provider = MeterProvider()
        else:
            provider = MeterProvider()
        _otel_metrics.set_meter_provider(provider)
        _meter = _otel_metrics.get_meter("coding-agent")
    except Exception as e:  # pragma: no cover
        logger.warning("Meter init failed (%s) — using no-op", e)
        _meter = _NoOpMeter()
    return _meter


def reset_meter() -> None:
    """Reset singleton (for tests)."""
    global _meter, _metrics
    _meter = None
    _metrics = None


# ── AgentMetrics — the 4 core metrics ────────────────────────────


class AgentMetrics:
    """Container for the 4 core engine metrics. One per process."""

    def __init__(self, meter: Optional[Any] = None):
        m = meter or init_meter()
        self.tool_call_counter = m.create_counter(
            "agent_tool_call_total",
            description="Total tool calls",
            unit="1",
        )
        self.tool_duration = m.create_histogram(
            "agent_tool_duration_ms",
            description="Tool execution duration in milliseconds",
            unit="ms",
        )
        self.tool_failure_counter = m.create_counter(
            "agent_tool_failure_total",
            description="Total tool failures",
            unit="1",
        )
        self.token_usage_counter = m.create_counter(
            "agent_token_usage_total",
            description="Total LLM tokens consumed",
            unit="1",
        )

    def record_tool_call(self, tool: str, duration_ms: float, success: bool) -> None:
        status = "ok" if success else "fail"
        self.tool_call_counter.add(1, {"tool": tool, "status": status})
        self.tool_duration.record(max(0.0, float(duration_ms)), {"tool": tool})
        if not success:
            self.tool_failure_counter.add(1, {"tool": tool})

    def record_tokens(self, input_tokens: int, output_tokens: int, model: str) -> None:
        if input_tokens:
            self.token_usage_counter.add(input_tokens, {"type": "input", "model": model})
        if output_tokens:
            self.token_usage_counter.add(output_tokens, {"type": "output", "model": model})


_metrics: Optional[AgentMetrics] = None


def get_metrics() -> AgentMetrics:
    """Return process-wide AgentMetrics singleton."""
    global _metrics
    if _metrics is None:
        _metrics = AgentMetrics()
    return _metrics
