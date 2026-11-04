"""The SDK's log lines, visible under uvicorn.

uvicorn configures its own loggers (`uvicorn`, `uvicorn.error`,
`uvicorn.access`) and leaves the root logger without a handler, so an INFO
line from `igni_service` — "uploaded extension …", "refused a token: …" —
reached nobody, and a WARNING came out as Python's bare last-resort line.
A service that configures logging itself (a root handler, or one on
`igni_service`) is left exactly as it configured it.
"""

from __future__ import annotations

import logging
import sys

LOGGER = "igni_service"


class ServiceLogs:
    """Gives `igni_service`'s lines a handler when nothing else would show them."""

    #: uvicorn's shape: `INFO:     igni: uploaded extension …`.
    FORMAT = "%(levelname)-9s %(message)s"

    @classmethod
    def make_visible(cls) -> bool:
        """Attach a stderr handler at INFO if neither `igni_service` nor the root has one.

        Answers whether it did. Called by `IgniService.lifespan`; safe to
        call more than once.
        """
        logger = logging.getLogger(LOGGER)
        if logger.handlers or logging.getLogger().handlers:
            return False
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_Uvicornish(cls.FORMAT))
        logger.addHandler(handler)
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)
        return True


class _Uvicornish(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record = logging.makeLogRecord(record.__dict__)
        record.levelname = f"{record.levelname}:"
        return super().format(record)
