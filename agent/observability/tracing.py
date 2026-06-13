"""Tracing primitives (PR-10).

When `opentelemetry` is installed, exports proper spans to OTLP or console.
When it isn't, provides zero-cost no-op shims so the engine can call
`tracer.start_span(...)` unconditionally.

P14-1 default behavior (X1 + X3 — stdout + local JSONL file):
- If SDK installed AND no OTLP endpoint: dual-export spans to both
  ConsoleSpanExporter (stdout, immediate visibility) and a local JSONL file
  exporter (`~/.coding-agent/otel/{date}.jsonl`).
- If SDK installed AND `OTEL_EXPORTER_OTLP_ENDPOINT` set: OTLP gRPC + file
  (stdout suppressed; production users don't want stdout noise).
- Set `OTEL_DISABLE_FILE_EXPORTER=1` to disable the local file.
- Set `OTEL_DISABLE_CONSOLE_EXPORTER=1` to disable stdout export.
- If SDK missing: W1 warning emitted on first `init_tracer()` call, then
  a no-op tracer is returned.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


try:
    # Optional dependency. We only need the API surface in the engine.
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when otel isn't installed
    OTEL_AVAILABLE = False
    _otel_trace = None  # type: ignore


# ── JSONL file exporter (P14-1 X3) ──────────────────────────────


if OTEL_AVAILABLE:

    class JsonlFileSpanExporter(SpanExporter):
        """Append-only JSONL file exporter (industry-standard structured logs).

        Writes one span per line as JSON to `~/.coding-agent/otel/{date}.jsonl`.
        Thread-safe; safe to call from multiple async tasks. Designed for
        post-hoc analysis with `jq` / pandas / any log-aggregator.

        Each line is a self-contained JSON object with:
            - timestamp (ISO 8601 UTC)
            - name, context.trace_id, context.span_id
            - attributes (flat dict)
            - status (OK / ERROR)
            - events (list of timestamped events)
            - duration_ns

        Format follows the OTel log data model conventions; compatible with
        Jaeger / Tempo / Loki / OpenObserve parsers that handle JSONL.
        """

        def __init__(self, output_dir: Optional[Path] = None):
            self.output_dir = Path(output_dir or (Path.home() / ".coding-agent" / "otel"))
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._lock = threading.Lock()
            self._file_handle = None
            self._current_date = ""

        def _get_file(self):
            """Return today's file handle, rotating on date change."""
            today = datetime.now().strftime("%Y-%m-%d")
            if self._current_date != today or self._file_handle is None:
                if self._file_handle is not None:
                    try:
                        self._file_handle.close()
                    except OSError:
                        pass
                path = self.output_dir / f"{today}.jsonl"
                self._file_handle = open(path, "a", encoding="utf-8", buffering=1)  # line-buffered
                self._current_date = today
            return self._file_handle

        def export(self, spans) -> SpanExportResult:  # type: ignore[override]
            """Append each span as a JSON line."""
            with self._lock:
                try:
                    fh = self._get_file()
                    for span in spans:
                        record = self._span_to_dict(span)
                        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                except Exception as e:
                    logger.warning("JsonlFileSpanExporter.export failed: %s", e)
                    return SpanExportResult.FAILURE
            return SpanExportResult.SUCCESS

        def _span_to_dict(self, span) -> dict:
            """Convert an OTel ReadableSpan to a JSONL-friendly dict."""
            ctx = span.get_span_context()
            parent_id = ""
            parent = getattr(span, "parent", None)
            if parent is not None:
                try:
                    parent_id = format(parent.span_id, "016x")
                except Exception:
                    parent_id = ""
            attrs = dict(getattr(span, "attributes", {}) or {})
            events: List[dict] = []
            for ev in getattr(span, "events", []) or []:
                events.append(
                    {
                        "name": ev.name,
                        "timestamp": (
                            datetime.fromtimestamp(ev.timestamp / 1e9, tz=None).isoformat()
                            if ev.timestamp
                            else None
                        ),
                        "attributes": dict(getattr(ev, "attributes", {}) or {}),
                    }
                )
            try:
                start_ns = span.start_time
                end_ns = span.end_time if hasattr(span, "end_time") else start_ns
                duration_ns = max(0, int(end_ns) - int(start_ns))
            except Exception:
                duration_ns = 0
            return {
                "timestamp": (
                    datetime.fromtimestamp(
                        span.start_time / 1e9 if span.start_time else 0, tz=None
                    ).isoformat()
                    if span.start_time
                    else None
                ),
                "name": span.name,
                "trace_id": format(ctx.trace_id, "032x") if ctx and ctx.trace_id else "",
                "span_id": format(ctx.span_id, "016x") if ctx and ctx.span_id else "",
                "parent_span_id": parent_id,
                "kind": str(span.kind) if hasattr(span, "kind") else "",
                "status": str(span.status.status_code) if hasattr(span, "status") else "",
                "attributes": attrs,
                "events": events,
                "duration_ns": duration_ns,
            }

        def shutdown(self) -> None:
            with self._lock:
                if self._file_handle is not None:
                    try:
                        self._file_handle.close()
                    except OSError:
                        pass
                    self._file_handle = None

else:

    class JsonlFileSpanExporter:  # type: ignore[no-redef]
        """Stub when OTel SDK is missing — never instantiated."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "JsonlFileSpanExporter requires opentelemetry-sdk. "
                "Install with `pip install -e .[observability]`."
            )


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
    use_console: Optional[bool] = None,
    file_exporter: Optional["JsonlFileSpanExporter"] = None,
) -> Any:
    """Initialise the global tracer. Idempotent.

    P14-1 default behavior:
    - If OTel is not installed: emit W1 warning + return a no-op tracer.
    - If OTel is installed and `OTEL_EXPORTER_OTLP_ENDPOINT` is set (or
      `otlp_endpoint` arg provided): export to OTLP gRPC + JSONL file.
      Stdout is suppressed to avoid log noise in production.
    - If OTel is installed and no OTLP endpoint: export to BOTH stdout
      (ConsoleSpanExporter, immediate visibility for local dev) AND
      JSONL file (`~/.coding-agent/otel/{date}.jsonl`).
    - Override the defaults via:
        `OTEL_DISABLE_FILE_EXPORTER=1` — disable file
        `OTEL_DISABLE_CONSOLE_EXPORTER=1` — disable stdout
    """
    global _tracer
    if _tracer is not None:
        return _tracer
    if not OTEL_AVAILABLE:
        # P14-1 W1: explicit warning, no longer silent no-op
        logger.warning(
            "opentelemetry-sdk is not installed; tracing is disabled. "
            "Install with `pip install -e .[observability]` (~5MB SDK + OTLP) "
            "to enable span export to stdout and ~/.coding-agent/otel/{date}.jsonl."
        )
        _tracer = _NoOpTracer()
        return _tracer
    try:
        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)
        # Determine endpoint
        if otlp_endpoint is None:
            otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        disable_console = os.getenv("OTEL_DISABLE_CONSOLE_EXPORTER", "0") == "1"
        disable_file = os.getenv("OTEL_DISABLE_FILE_EXPORTER", "0") == "1"

        # ── OTLP path (production) ──
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
                )
            except ImportError:
                logger.warning("OTLP exporter not installed; skipping OTLP export")
        else:
            # ── Local dev path: stdout + JSONL file ──
            if use_console is None:
                use_console = not disable_console
            if use_console:
                provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

        # ── JSONL file exporter (always added unless disabled) ──
        if not disable_file:
            if file_exporter is None:
                file_exporter = JsonlFileSpanExporter()
            provider.add_span_processor(SimpleSpanProcessor(file_exporter))

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
