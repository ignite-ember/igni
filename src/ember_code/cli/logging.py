"""Debug logging bootstrap.

Owns the :class:`RotatingFileHandler` lifecycle and the
``logging.root`` mutation sequence in one place so the CLI
callback doesn't reach into the root logger inline. Bootstrap is
still module-mutating by nature — you can't add a handler to the
root logger without touching global state — but wrapping it in a
class gives the mutation a single named owner (grep for
``DebugLogging.enable`` and you'll find every path that flips
root-logger state on the CLI startup path).
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


class DebugLogging:
    """Bootstrap for the ``ember --debug`` file-log surface."""

    DEFAULT_PATH = Path.home() / ".ember" / "debug.log"
    _FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
    _MAX_BYTES = 10_000_000
    _BACKUPS = 2

    @classmethod
    def enable(cls, path: Path | None = None) -> Path:
        """Attach a rotating file handler to the root logger and
        return the log-file path.

        Called from the CLI callback when ``--debug`` is passed.
        Replacing the root logger's handler list (rather than
        appending) matches the pre-refactor behavior — the debug
        session wants a clean slate so noisy imports don't pollute
        the trace.
        """
        log_path = path if path is not None else cls.DEFAULT_PATH
        log_path.parent.mkdir(parents=True, exist_ok=True)

        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=cls._MAX_BYTES,
            backupCount=cls._BACKUPS,
        )
        handler.setFormatter(logging.Formatter(cls._FORMAT))

        logging.root.handlers.clear()
        logging.root.addHandler(handler)
        logging.root.setLevel(logging.DEBUG)
        logging.getLogger("ember_code").setLevel(logging.DEBUG)

        return log_path
