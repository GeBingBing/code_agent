"""OpenTelemetry hook (PR-19, extracted from AgentEngine).

Four hooks for tracing + metrics:
  - `before_tool` / `after_tool`: span around tool.execute
  - `before_llm` / `after_llm`: span around llm.call

Spans carry timing attributes; metrics record token counts and tool
durations. All exceptions swallowed — telemetry must never break the
agent. Originally `AgentEngine._otel_*` methods.
"""

from __future__ import annotations

import time
from typing import Any


class OtelHook:
    """OpenTelemetry spans + metrics for tool and LLM calls.

    Constructor takes the tracer, metrics recorder, session/trace id,
    and (model, provider) for LLM-call attributes.
    """

    def __init__(self, tracer, metrics, trace_id: str, model: str, provider: str):
        self._tracer = tracer
        self._metrics = metrics
        self._trace_id = trace_id
        self._model = model or ""
        self._provider = provider or ""

    # ── Tool spans ────────────────────────────────────────────────

    async def before_tool(self, payload: Any) -> Any:
        if self._tracer is None or not isinstance(payload, dict):
            return payload
        try:
            span = self._tracer.start_span(
                "tool.execute",
                attributes={
                    "tool.name": payload.get("tool", ""),
                    "agent.session_id": self._trace_id,
                },
            )
            payload["_otel_span"] = span
            payload["_otel_start_ts"] = time.time()
        except Exception:
            pass
        return payload

    async def after_tool(self, payload: Any) -> Any:
        if self._tracer is None or not isinstance(payload, dict):
            return payload
        span = payload.get("_otel_span")
        start = payload.get("_otel_start_ts")
        result = payload.get("result")
        error = payload.get("error")
        success = error is None and (
            getattr(result, "success", True) if result is not None else True
        )
        duration_ms = 0.0
        if isinstance(start, (int, float)):
            duration_ms = (time.time() - start) * 1000.0
        if span is not None:
            try:
                span.set_attribute("tool.duration_ms", duration_ms)
                span.set_attribute("tool.success", success)
                if error:
                    span.set_attribute("tool.error", str(error)[:200])
                span.end()
            except Exception:
                pass
        if self._metrics is not None:
            try:
                self._metrics.record_tool_call(
                    tool=payload.get("tool", "unknown"),
                    duration_ms=duration_ms,
                    success=success,
                )
            except Exception:
                pass
        return payload

    # ── LLM spans ─────────────────────────────────────────────────

    async def before_llm(self, payload: Any) -> Any:
        if self._tracer is None or not isinstance(payload, dict):
            return payload
        messages = payload.get("messages")
        try:
            span = self._tracer.start_span(
                "llm.call",
                attributes={
                    "llm.model": self._model,
                    "llm.provider": self._provider,
                    "llm.message_count": len(messages) if isinstance(messages, list) else 0,
                    "agent.session_id": self._trace_id,
                },
            )
            payload["_otel_llm_span"] = span
            payload["_otel_llm_start"] = time.time()
        except Exception:
            pass
        return payload

    async def after_llm(self, payload: Any) -> Any:
        if self._tracer is None or not isinstance(payload, dict):
            return payload
        span = payload.get("_otel_llm_span")
        start = payload.get("_otel_llm_start")
        if span is not None:
            try:
                if isinstance(start, (int, float)):
                    span.set_attribute("llm.duration_ms", (time.time() - start) * 1000.0)
                usage = payload.get("usage") or {}
                if isinstance(usage, dict):
                    if "input_tokens" in usage:
                        span.set_attribute("llm.input_tokens", int(usage["input_tokens"]))
                    if "output_tokens" in usage:
                        span.set_attribute("llm.output_tokens", int(usage["output_tokens"]))
                span.end()
            except Exception:
                pass
        # Metrics: record token counts when available
        if self._metrics is not None:
            usage = payload.get("usage") or {}
            if isinstance(usage, dict):
                try:
                    self._metrics.record_tokens(
                        input_tokens=int(usage.get("input_tokens", 0)),
                        output_tokens=int(usage.get("output_tokens", 0)),
                        model=self._model or "unknown",
                    )
                except Exception:
                    pass
        return payload
