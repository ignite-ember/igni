"""Direct-write HITL trace helper.

Extracted from :mod:`ember_code.backend.server_pause` where it was a
module-level ``_HITL_TRACE_PATH`` + free ``_hitl_trace`` function
(one of the AP1 module-level-mutable-state offenders in the audit).

The trace bypasses the standard ``logging`` pipeline because the
pipeline is silenced under several test / production configurations
and this file has been the fastest way to confirm the multiplexer is
running. Cheap: one flushed write per pump iteration, no rotation
needed (dev machines only).

Why a class:

* Tests can inject ``HITLTracer(path=tmp_path / 'trace.log',
  enabled=False)`` (or ``enabled=True`` and read the file) instead
  of touching ``~/.ember``.
* :class:`HITLStreamMultiplexer` gets it via constructor injection
  so no reach-back into a module-level global.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Path resolution happens at import time — no I/O until ``.trace()``
# fires (which calls ``mkdir(parents=True, exist_ok=True)``).
_DEFAULT_PATH = Path(os.path.expanduser("~/.ember/hitl_trace.log"))


class HITLTracer:
    """Best-effort direct-write trace for the pause multiplexer."""

    def __init__(self, path: Path | None = None, enabled: bool = True) -> None:
        """Construct a tracer.

        ``path`` defaults to ``~/.ember/hitl_trace.log`` when not
        specified. ``enabled=False`` turns :meth:`trace` into a
        no-op — used by tests that don't want a real file to be
        touched but keep the injection shape consistent.
        """
        self._path = path or _DEFAULT_PATH
        self._enabled = enabled

    @classmethod
    def from_settings(cls, settings: object | None = None) -> HITLTracer:
        """Factory reading the trace path from ``settings``.

        Kept for symmetry with the new pause pipeline's other
        classes; the current ``Settings`` doesn't yet carry a trace
        path so we fall back to the default. The signature accepts
        ``settings`` so a future field addition is a one-line
        change here.
        """
        del settings  # reserved for future use
        return cls(path=_DEFAULT_PATH, enabled=True)

    def trace(self, text: str) -> None:
        """Append one line to the trace file, or no-op if disabled.

        Swallows every failure — a trace-write failure MUST NEVER
        break the live multiplexer. Mirrors the pre-refactor
        ``_hitl_trace`` behaviour exactly.
        """
        if not self._enabled:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {text}\n")
        except Exception:
            # Best-effort — never let a trace-write failure surface.
            pass
