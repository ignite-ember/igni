"""Logs in the cloud: JSON lines on standard output.

In the cloud (:class:`igni_cloud.layer.CloudLayer`) nothing writes a log
file: the node collects the VM's standard output. Every logger that
writes a file on the desktop writes here instead, one JSON object per
line — ``ts``, ``level``, ``logger``, ``message``, and whatever
structured ``fields`` a record carries (the audit log's entries, say).

Written by a thread of its own, never by the code that logs: in a VM
standard output is the serial console, where each byte is a trap to the
host, and a line written from the event loop held every request behind
it — on a resume, the first answer among them. The loop only queues the
record (:class:`StdoutLog`); :meth:`StdoutLog.flush` waits for the
thread to have written what was queued.

After a restore the writer is not even woken until the first answer
and the burst of requests behind it are through
(:meth:`StdoutLog.hold`, :meth:`StdoutLog.release`): formatting a line
takes the interpreter's lock from the event loop in its turn, and walks
the logging, JSON and datetime code — pages a restored VM brings back
one fault at a time, on the first answer's path. The records keep their
times and order; they are written a moment later.
"""

from __future__ import annotations

import atexit
import copy
import json
import logging
import logging.handlers
import queue
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
        elif record.exc_text:
            line["exception"] = record.exc_text
        return json.dumps(line, default=str)


class QueuedRecords(logging.handlers.QueueHandler):
    """Queues a record whole — its fields, and its exception as text — for the writer."""

    #: Held at most this many records; past it they are queued as they come.
    MAX_HELD: ClassVar[int] = 1000

    def __init__(self, records: queue.Queue[logging.LogRecord]) -> None:
        super().__init__(records)
        self._held: list[logging.LogRecord] | None = None

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        prepared = copy.copy(record)
        prepared.msg = record.getMessage()
        prepared.args = None
        if record.exc_info:
            prepared.exc_text = logging.Formatter().formatException(record.exc_info)
            prepared.exc_info = None
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        held = self._held
        if held is not None and len(held) < self.MAX_HELD:
            held.append(record)
            return
        super().enqueue(record)

    def hold_writer(self) -> None:
        """Keep what is logged from the writer until :meth:`wake_writer`.

        (Not ``hold``/``release``: :class:`logging.Handler` has a ``release`` of its own, its lock's.)
        """
        with self.lock:  # type: ignore[union-attr]
            if self._held is None:
                self._held = []

    def wake_writer(self) -> None:
        """Hand what was held to the writer, in order."""
        with self.lock:  # type: ignore[union-attr]
            held, self._held = self._held, None
            for record in held or ():
                super().enqueue(record)


class StdoutLog:
    """The process's one standard-output handler, and the loggers it serves."""

    _lock: ClassVar[threading.Lock] = threading.Lock()
    _handler: ClassVar[QueuedRecords | None] = None
    _queue: ClassVar[queue.Queue[logging.LogRecord] | None] = None
    _writer: ClassVar[logging.handlers.QueueListener | None] = None

    @classmethod
    def handler(cls) -> logging.Handler:
        """What loggers get: a handler that queues; a thread writes the line."""
        with cls._lock:
            if cls._handler is None:
                stream = logging.StreamHandler(sys.stdout)
                stream.setFormatter(JsonLineFormatter())
                records: queue.Queue[logging.LogRecord] = queue.Queue()
                writer = logging.handlers.QueueListener(records, stream)
                writer.start()
                cls._queue, cls._writer = records, writer
                cls._handler = QueuedRecords(records)
            return cls._handler

    @classmethod
    def hold(cls) -> None:
        """Queue nothing for the writer until :meth:`release` (after a restore's first answer)."""
        if cls._handler is not None:
            cls._handler.hold_writer()

    @classmethod
    def release(cls) -> None:
        """Let the writer have what was held."""
        if cls._handler is not None:
            cls._handler.wake_writer()

    @classmethod
    def flush(cls) -> None:
        """Until every record queued so far is written."""
        records = cls._queue
        if records is not None:
            records.join()

    @classmethod
    def reset(cls) -> None:
        """Write what is queued and forget the handler, so the next one
        writes to the current ``sys.stdout`` (tests capture it)."""
        with cls._lock:
            if cls._handler is not None:
                cls._handler.wake_writer()
            writer = cls._writer
            cls._handler = cls._queue = cls._writer = None
        if writer is not None:
            writer.stop()

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


# What was logged last is written before the process ends.
atexit.register(StdoutLog.reset)


__all__ = ["JsonLineFormatter", "QueuedRecords", "StdoutLog"]
