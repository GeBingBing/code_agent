"""Tracing primitives (PR-10).

When `opentelemetry` is installed, exports proper spans to OTLP or console.
When it isn't, provides zero-cost no-op shims so the engine can call
`tracer.start_span(...)` unconditionally.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


try:
    # Optional dependency. We only need the API surface in the engine.
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when otel isn't installed
    OTEL_AVAILABLE = False
    _otel_trace = None  # type: ignore


# ── No-op shims ───────────────────────────────────────────────────


class _NoOpSpan:
    """Minimum surface used by the engine: set_attribute / end / context manager."""

    __slots__ = ("_name", "_attrs", "_ended")

    def __init__(self, name: str = "", attributes: Optional[Dict[str, Any]] = None):
        self._name = name
        self._attrs: Dict[str, Any] = dict(attributes or {})
        self._ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        self._attrs[key] = value

    def set_status(self, *args, **kwargs) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        # Stored as attribute so tests can introspect
        self._attrs["exception"] = repr(exception)

    def end(self) -> None:
        self._ended = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self.record_exception(exc)
        self.end()
        return False

    # Diagnostics for tests
    @property
    def attributes(self) -> Dict[str, Any]:
        return dict(self._attrs)

    @property
    def name(self) -> str:
        return self._name

    @property
    def ended(self) -> bool:
        return self._ended


class _NoOpTracer:
    """A tracer that produces _NoOpSpan instances. Drop-in for otel.Tracer."""

    def start_span(self, name: str, attributes: Optional[Dict[str, Any]] = None, **_):
        return _NoOpSpan(name=name, attributes=attributes)

    def start_as_current_span(self, name: str, attributes: Optional[Dict[str, Any]] = None, **_):
        # Returns context-manager span
        return _NoOpSpan(name=name, attributes=attributes)


# ── Global tracer (singleton) ─────────────────────────────────────


_tracer: Optional[Any] = None


def init_tracer(
    service_name: str = "coding-agent",
    otlp_endpoint: Optional[str] = None,
    use_console: bool = False,
) -> Any:
    """Initialise the global tracer. Idempotent.

    - If OTel is not installed, returns a no-op tracer.
    - If `otlp_endpoint` is set, exports spans to OTLP gRPC.
    - Otherwise, falls back to console export when `use_console=True`,
      else a SDK tracer with no exporter (still usable for in-memory tests).
    """
    global _tracer
    if _tracer is not None:
        return _tracer
    if not OTEL_AVAILABLE:
        _tracer = _NoOpTracer()
        return _tracer
    try:
        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
                exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
                provider.add_span_processor(BatchSpanProcessor(exporter))
            except ImportError:
                logger.warning("OTLP exporter not installed; falling back to console")
                use_console = True
        if use_console:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        _otel_trace.set_tracer_provider(provider)
        _tracer = _otel_trace.get_tracer(service_name)
    except Exception as e:  # pragma: no cover
        logger.warning("Tracer init failed (%s) — using no-op", e)
        _tracer = _NoOpTracer()
    return _tracer


def get_tracer() -> Any:
    """Return the global tracer, initialising lazily if needed.

    Uses the `OTEL_EXPORTER_OTLP_ENDPOINT` env var when present.
    """
    global _tracer
    if _tracer is None:
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        init_tracer(otlp_endpoint=endpoint)
    return _tracer


def reset_tracer() -> None:
    """Reset the singleton (for tests)."""
    global _tracer
    _tracer = None
