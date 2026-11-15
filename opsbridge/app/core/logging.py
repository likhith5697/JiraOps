"""Structured logging with trace-id propagation and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
support_request_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "support_request_id", default=None
)


def new_trace_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def log_context(trace_id: str | None = None, support_request_id: int | None = None) -> Iterator[None]:
    """Bind tracing identifiers for the duration of a unit of work (works across threads)."""
    t1 = trace_id_var.set(trace_id or trace_id_var.get() or new_trace_id())
    t2 = support_request_id_var.set(support_request_id)
    try:
        yield
    finally:
        support_request_id_var.reset(t2)
        trace_id_var.reset(t1)


_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(basic\s+)[A-Za-z0-9+/]+=*"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)((?:client_secret|refresh_token|access_token|api_token|password|clientstate)"
            r"[\"']?\s*[:=]\s*[\"']?)[^\"'&\s,}]+"
        ),
        r"\1[REDACTED]",
    ),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get()
        record.support_request_id = support_request_id_var.get()
        return True


_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "trace_id",
    "support_request_id",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "trace_id": getattr(record, "trace_id", None),
        }
        if getattr(record, "support_request_id", None) is not None:
            payload["support_request_id"] = record.support_request_id
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        if extras:
            payload["extra"] = json.loads(redact(json.dumps(extras, default=str)))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s [%(trace_id)s] %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full URLs at INFO; keep it quieter.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
