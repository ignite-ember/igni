"""Best-effort browser opener utility.

Consolidates the two previous copies of an ``_open_in_browser``
helper that lived in :mod:`ember_code.backend.command_handler`
(module-level free function) and
:mod:`ember_code.backend.cmd_codeindex` (private static method on
:class:`CodeIndexCommand`). Both did exactly the same thing: try
:func:`webbrowser.open`, log-and-swallow anything that goes
wrong. Kept as a tiny stateless class so the entry point is a
method call (matches the project's OOP posture) rather than a
loose module function.
"""

from __future__ import annotations

import logging
import webbrowser

logger = logging.getLogger(__name__)


class BrowserOpener:
    """Wrap :func:`webbrowser.open` so failures never surface.

    A single ``@staticmethod`` because there's no per-instance
    state — every caller just wants "open this URL in the user's
    default browser, don't crash if that's not possible".
    """

    @staticmethod
    def open(url: str) -> None:
        """Best-effort open in browser; failures are logged, never raised."""
        try:
            webbrowser.open(url)
        except Exception as exc:  # pragma: no cover — platform-dependent
            logger.info("could not open browser for %s: %s", url, exc)
