"""Structured JSON logging (stdout) + persisted audit events and logs (database)."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from .models import AppLog, AuditEvent, now_iso

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_SECRET_HINTS = ("key", "token", "secret", "authorization", "password")
_logger = logging.getLogger("workbench")


def redact(fields: dict) -> dict:
    """Drop anything that looks like a secret. Callers also never pass record values, only ids, counts and codes."""
    return {k: ("[redacted]" if any(h in k.lower() for h in _SECRET_HINTS) else v) for k, v in fields.items()}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {"ts": now_iso(), "level": record.levelname.lower(), "logger": record.name, "message": record.getMessage()}
        base.update(getattr(record, "fields", {}))
        if record.exc_info:
            base["exception"] = self.formatException(record.exc_info)
        return json.dumps(base, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if not any(getattr(h, "_workbench", False) for h in root.handlers):
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        h._workbench = True  # type: ignore[attr-defined]
        root.addHandler(h)
    root.setLevel(level)


def log_event(db: Session | None, level: str, event: str, **fields: Any) -> None:
    """Emit one structured log line and, when a session is given, persist it (committed with the caller's transaction)."""
    f = redact({"event": event, "request_id": request_id_var.get(), **fields})
    getattr(_logger, level if level in ("debug", "info", "warning", "error") else "info")(event, extra={"fields": f})
    if db is not None:
        db.add(AppLog(level=level, event=event, request_id=request_id_var.get(), fields=redact(fields)))


def persist_log(db: Session, level: str, event: str, **fields: Any) -> None:
    """Persist a log row without re-emitting to stdout (used for entries already emitted live, e.g. agent tool calls)."""
    db.add(AppLog(level=level, event=event, request_id=request_id_var.get(), fields=redact(fields)))


def audit(db: Session, type_: str, actor: str, *, plan_version: int | None = None, run_ref: str | None = None, **detail: Any) -> None:
    db.add(AuditEvent(type=type_, actor=actor, request_id=request_id_var.get(), plan_version=plan_version, run_ref=run_ref, detail=redact(detail)))
    log_event(None, "info", "audit." + type_, actor=actor, plan_version=plan_version, run_ref=run_ref, **detail)


@contextmanager
def timed():
    t0 = time.perf_counter()
    box = {"ms": 0}
    try:
        yield box
    finally:
        box["ms"] = round((time.perf_counter() - t0) * 1000, 1)
