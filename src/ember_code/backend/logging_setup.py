"""The backend keeps a log, without being asked.

Until now it did not. ``--debug`` is unreachable — the desktop app
builds the backend's argv and nothing in the UI edits it — and the
``EMBER_DEBUG_LOG`` env var that replaced it still had to be set by
someone who already suspected a problem and knew the variable's name.
A user running the ``.app`` can do neither. So the shipped product
wrote no record of anything, anywhere, and its stdout goes to the app,
which reads it for the ready line and drops the rest.

That is the reason a string of failures each cost an afternoon: a
sidecar that exited 63 ms after starting, an attach gated on a
condition that could never be true, a cache probe killed at its own
deadline that wiped 519 MB. Each one logged something. Nothing was
listening.

So: INFO to ``~/.ember/logs/backend.log`` always, rotating so it cannot
grow without bound, and ``EMBER_DEBUG_LOG`` demoted from an on-switch
to a verbosity dial.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: Five files of 2 MB. Enough to hold a long session plus the couple of
#: restarts either side of it — the reconstruction almost always needs
#: the run *before* the one that went wrong.
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 4

_HANDLER_MARK = "_ember_backend_log"


def resolve_log_path() -> Path:
    """Where the log goes.

    ``EMBER_DEBUG_LOG_PATH`` still wins, because tests and support
    requests both want to put it somewhere specific.
    """
    override = os.environ.get("EMBER_DEBUG_LOG_PATH")
    if override:
        return Path(override)
    return Path.home() / ".ember" / "logs" / "backend.log"


def resolve_level(*, debug_flag: bool = False) -> int:
    """DEBUG when asked, INFO otherwise.

    ``EMBER_DEBUG_LOG`` used to decide whether logging happened at all.
    It now decides how much, which is the question people actually
    meant to ask when they set it.
    """
    if debug_flag or os.environ.get("EMBER_DEBUG_LOG"):
        return logging.DEBUG
    return logging.INFO


def configure_logging(*, debug_flag: bool = False, path: Path | None = None) -> Path | None:
    """Attach the rotating file handler. Returns the path, or ``None``
    if the log could not be opened.

    Never raises. A backend that cannot write its log is a backend with
    a diagnosis problem; a backend that *exits* because it cannot write
    its log is a backend with a much larger one, and read-only or full
    home directories are real.

    The handler goes on the ``ember_code`` package logger rather than
    the root, deliberately. Downstream imports (httpx, litellm, …)
    reset root handlers between startup and first use — when that
    happened, the root file handler went with them and the log stopped
    five lines in while the backend ran on for minutes.
    """
    log_path = path or resolve_log_path()
    level = resolve_level(debug_flag=debug_flag)

    pkg = logging.getLogger("ember_code")
    for existing in pkg.handlers:
        if getattr(existing, _HANDLER_MARK, False):
            existing.setLevel(level)
            pkg.setLevel(level)
            return log_path

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = _FlushingRotatingHandler(
            str(log_path),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        # No log today. Say so on stderr, which at least reaches a
        # terminal launch, and carry on.
        logging.getLogger(__name__).warning("could not open log file at %s", log_path)
        return None

    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    handler.setLevel(level)
    setattr(handler, _HANDLER_MARK, True)
    pkg.addHandler(handler)
    pkg.setLevel(level)
    return log_path


class _FlushingRotatingHandler(RotatingFileHandler):
    """Flush every record.

    Buffering makes ``tail -f`` lie, and every one of the incidents
    that motivated this file was diagnosed — eventually — by tailing a
    log while reproducing. A few thousand lines a session is not a
    throughput problem.
    """

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()
