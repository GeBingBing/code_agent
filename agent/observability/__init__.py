"""Observability subpackage (PR-10).

Provides OpenTelemetry-compatible tracing, metrics, and structured logging
with graceful degradation when OTel SDKs are not installed.

Why optional?
- OTel pulls in ~20MB of transitive deps and requires a collector to be
  useful. We don't want to force this on every user.
- The no-op fallbacks are zero-cost (object pool of `_NoOpSpan` reused).
- When OTel *is* installed and an OTLP endpoint is configured, full
  traces/metrics/logs flow without any code changes.

Detection: try to import `opentelemetry`. If it succeeds, real APIs are
used. If not, the in-module no-op shims take over.
"""

from .tracing import init_tracer, get_tracer, OTEL_AVAILABLE
from .metrics import init_meter, get_metrics, AgentMetrics
from .logging import JSONFormatter, setup_logging

__all__ = [
    "init_tracer",
    "get_tracer",
    "init_meter",
    "get_metrics",
    "AgentMetrics",
    "JSONFormatter",
    "setup_logging",
    "OTEL_AVAILABLE",
]
