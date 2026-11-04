"""Open a file with whatever handles it on this machine.

The sibling of :mod:`ember_code.backend.browser_opener`, and the same
posture: best effort, never raise, say whether it worked.

Kept separate from that module because the failure modes differ.
``webbrowser.open`` returns ``True`` on macOS whether or not anything
handled the URL, so "did it work" has to be answered by the launcher's
exit status instead — which means per-platform code, which is the
whole content of this file.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

#: Long enough for a launcher to hand off, short enough that a hung
#: one does not hold an RPC open. The launchers below return as soon
#: as the receiving application has been told; none of them wait for
#: it to finish starting.
LAUNCH_TIMEOUT_SECONDS: float = 10.0


class FileOpener:
    """Ask the desktop to open a path.

    Blocking, so callers on an event loop should reach it through
    ``asyncio.to_thread`` — the launchers are short-lived, but
    "short-lived" is not "instant" and the loop serves every other
    session too.
    """

    @staticmethod
    def open(path: Path) -> bool:
        """Open ``path``. ``True`` if the desktop accepted it."""
        try:
            if sys.platform == "darwin":
                return FileOpener._run(["open", str(path)])
            if sys.platform == "win32":
                # No launcher binary to exit-code: ``startfile`` raises
                # on failure and returns nothing on success.
                os.startfile(str(path))  # noqa: S606 — the path is validated by the caller
                return True
            return FileOpener._run(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001 — platform-dependent, never fatal
            logger.info("could not open %s: %s", path, exc)
            return False

    @staticmethod
    def _run(argv: list[str]) -> bool:
        """Run a launcher, reporting whether it succeeded."""
        try:
            result = subprocess.run(  # noqa: S603 — argv is built here, never from input
                argv,
                check=False,
                capture_output=True,
                timeout=LAUNCH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.info("launcher %s failed: %s", argv[0], exc)
            return False
        if result.returncode != 0:
            logger.info(
                "launcher %s exited %s: %s",
                argv[0],
                result.returncode,
                result.stderr.decode(errors="replace").strip(),
            )
            return False
        return True
