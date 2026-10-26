"""Logs in the cloud: JSON lines on standard output.

In the cloud profile (:mod:`ember_code.core.profile`) nothing writes a
log file: the node collects the VM's standard output. Every logger that
writes a file on the desktop writes here instead, one JSON object per
line — ``ts``, ``level``, ``logger``, ``message``, and whatever
structured ``fields`` a record carries (the audit log's entries, say).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import UTC, datetime
from typing import Any, ClassVar


class JsonLineFormatter(logging.Formatter):
    """One JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            line.update(fields)
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


class StdoutLog:
    """The process's one standard-output handler, and the loggers it serves."""

    _lock: ClassVar[threading.Lock] = threading.Lock()
    _handler: ClassVar[logging.Handler | None] = None

    @classmethod
    def handler(cls) -> logging.Handler:
        with cls._lock:
            if cls._handler is None:
                handler = logging.StreamHandler(sys.stdout)
                handler.setFormatter(JsonLineFormatter())
                cls._handler = handler
            return cls._handler

    @classmethod
    def reset(cls) -> None:
        """Forget the handler, so the next one writes to the current
        ``sys.stdout`` (tests capture it)."""
        with cls._lock:
            cls._handler = None

    @classmethod
    def attach(cls, name: str, level: int = logging.INFO) -> logging.Logger:
        """Send ``name``'s records to standard output (once)."""
        target = logging.getLogger(name)
        handler = cls.handler()
        if handler not in target.handlers:
            target.addHandler(handler)
        if target.level == logging.NOTSET or target.level > level:
            target.setLevel(level)
        return target


__all__ = ["JsonLineFormatter", "StdoutLog"]
