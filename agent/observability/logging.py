"""Structured JSON logging (PR-10).

Single-line JSON-per-record format that's parser-friendly for any
log shipper (Vector, Filebeat, Promtail, etc.). OpenTelemetry's
log API integrates with this transparently when the OTel SDK is
installed and configured.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """One JSON object per log line.

    Fields: ts (UTC ISO), level, logger, message, module, func, line.
    Extra attributes set via `logger.info("msg", extra={"key": "val"})`
    are merged in unless they collide with reserved fields.
    """

    RESERVED = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }
        # Merge any user-supplied extras
        for k, v in record.__dict__.items():
            if k in self.RESERVED or k.startswith("_"):
                continue
            if k in out:
                continue
            try:
                json.dumps(v)  # Test serialisability
                out[k] = v
            except (TypeError, ValueError):
                out[k] = repr(v)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", stream=None) -> logging.Handler:
    """Replace root handlers with a single JSONFormatter-backed stream handler.

    Returns the new handler so tests can inspect it. `stream` defaults
    to stderr; pass an io.StringIO to capture in tests.
    """
    handler = logging.StreamHandler(stream=stream)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    # Don't blow away every handler — only replace the ones we own.
    root.handlers = [
        h for h in root.handlers if not isinstance(getattr(h, "formatter", None), JSONFormatter)
    ] + [handler]
    root.setLevel(level)
    return handler
