"""Fails the run when the tests leave files in the repository root.

A test that hands a ``MagicMock`` or an engine object to code expecting a
path writes a file or directory named after it — ``MagicMock/``,
``<sqlalchemy.ext.asyncio.engine.AsyncEngine object at 0x…>`` — into the
working directory, which is the repository root. Both are in this
checkout's root today, from runs months apart; ``.gitignore`` hides them
(``MagicMock/``, ``<*object at 0x*>``), so nothing ever said a test was
writing outside its ``tmp_path``.

:class:`RootGuard` takes the root's top-level entries when the session
starts and compares at the end. A new entry is a stray unless it is a
tool's own cache; an entry already there that looks like a stringified
object and changed during the run is one too, so a leftover from an old
run does not hide the same leak happening again.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

#: What tools put in the root on their own: caches and coverage data.
TOOL_ENTRIES = re.compile(
    r"^(\.pytest_cache|\.mypy_cache|\.ruff_cache|\.hypothesis|__pycache__|\.coverage(\..*)?|"
    r"htmlcov|coverage\.xml|\.venv|\.DS_Store)$"
)

#: A name that is an object's repr rather than a path someone chose.
LOOKS_LIKE_AN_OBJECT = re.compile(r"Mock|object at 0x|^<.*>$")


class RootGuard(BaseModel):
    root: Path
    #: Top-level name → modification time, at the start.
    before: dict[str, float]

    @classmethod
    def taking(cls, root: Path) -> RootGuard:
        return cls(root=root, before=cls._entries(root))

    @staticmethod
    def _entries(root: Path) -> dict[str, float]:
        found: dict[str, float] = {}
        for entry in root.iterdir():
            try:
                found[entry.name] = entry.lstat().st_mtime
            except OSError:
                continue
        return found

    def strays(self) -> list[str]:
        """Entries the run left in the root, by name."""
        now = self._entries(self.root)
        new = [n for n in now if n not in self.before and not TOOL_ENTRIES.match(n)]
        touched = [
            n
            for n, mtime in now.items()
            if n in self.before and mtime != self.before[n] and LOOKS_LIKE_AN_OBJECT.search(n)
        ]
        return sorted(new + touched)

    def report(self, strays: list[str]) -> str:
        listed = "\n".join(f"  {self.root / name}" for name in strays)
        return (
            "The test run left files in the repository root:\n"
            f"{listed}\n"
            "A test wrote outside its tmp_path — usually a mock or an object "
            "passed where a path was expected. Find it, fix it, and delete these. "
            "(A file you created yourself while the suite ran counts too.)"
        )
