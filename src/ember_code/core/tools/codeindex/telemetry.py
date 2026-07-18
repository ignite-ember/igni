"""Eval telemetry sink for the codeindex toolkit.

Extracted from :class:`CodeIndexTools` so the file-append side-effect
lives behind a named class instead of a raw ``open()`` inside a
staticmethod. Activated by the ``EMBER_EVAL_TELEMETRY_PATH`` env
var — when unset, :meth:`TelemetryLog.record` is a cheap no-op.

The path is resolved ONCE at construction time. Callers (the eval
runner) set the env var before launching the process, so late-set
values are not a supported flow. If that ever changes, resolve
lazily on first :meth:`record` instead.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ember_code.core.tools.codeindex.schemas import TelemetryEntry

logger = logging.getLogger(__name__)


class TelemetryLog:
    """Best-effort append-only JSON-lines log of tool invocations.

    One instance per toolkit. Reads ``EMBER_EVAL_TELEMETRY_PATH`` once
    at construction; if unset, the class becomes a no-op. Any I/O
    error is logged at debug (never raised) — a failed telemetry write
    must not break a real tool call.
    """

    _ENV_VAR = "EMBER_EVAL_TELEMETRY_PATH"

    def __init__(self, path: Path | str | None = None) -> None:
        resolved = path if path is not None else os.environ.get(self._ENV_VAR)
        self._path: Path | None = Path(resolved) if resolved else None

    @property
    def enabled(self) -> bool:
        """True iff a log path is configured (``EMBER_EVAL_TELEMETRY_PATH`` set)."""
        return self._path is not None

    def record(self, entry: TelemetryEntry) -> None:
        """Append ``entry`` as one JSON line to the configured log file.

        No-op when telemetry is not configured. Any :class:`OSError`
        (permissions, disk full, missing parent directory) is captured
        and logged at debug — the toolkit swallows the failure to
        preserve the "telemetry never breaks a real call" invariant.
        """
        if self._path is None:
            return
        try:
            with self._path.open("a") as fh:
                fh.write(entry.model_dump_json() + "\n")
        except OSError:
            logger.debug("codeindex telemetry write failed", exc_info=True)
