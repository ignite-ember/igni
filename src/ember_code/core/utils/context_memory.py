"""Per-project auto-memory — MEMORY.md index loading + write-back
instructions the agent needs at session start.

Extracted from :mod:`ember_code.core.utils.context` per
CODE_STANDARDS.md Pattern 8 (small modules, one responsibility). The
parent module was 778 LoC of hierarchical rules loading with
memory-index handling glued into the middle; this file holds the
memory-index concern only.

## OOP shape

The dominant subject of this file is ``project_dir`` — every helper
took it as first arg and either derived paths from it or read files
under those paths. That's the classic Rule 6 "implicit subject
cluster" audit flag. The refactor promotes ``project_dir`` to
constructor state on :class:`ProjectMemoryBank`, with the three
public verbs (``ensure`` / ``load_index`` / ``writeback_instructions``)
as instance methods.

Free-function shims (:func:`ensure_memory_dir`,
:func:`load_memory_index`, :func:`memory_writeback_instructions`)
survive at the module bottom as one-liner delegations to the class
— they're what :mod:`context` re-exports and what the test suite
imports. Rewriting every call site to the class form is a separate
concern from the OOP promotion happening here.

## What lives here

- **Path resolution** — encode a project dir as a CC-compatible slug,
  resolve the ember-native + Claude Code cross-tool memory dirs.
- **``MEMORY.md`` reading** — with the 200-line / 25-KB cap Claude Code
  publishes, so a runaway memory file can never blow up the session
  prompt.
- **Write-back instructions** — the system-prompt block the agent
  sees at session start, delegated to
  :class:`context_memory_prompt.MemoryWritebackPrompt` so the 79-line
  template lives in its own file (Pattern 8).
- **Bootstrap** — :meth:`ProjectMemoryBank.ensure` creates the per-
  project memory directory idempotently so the agent's first
  ``save_file`` doesn't fail on a missing parent.

Backwards-compatible: :mod:`context` still re-exports the free-
function names and the leading-underscore private helpers; tests
that ``monkeypatch.setattr`` ``_ember_project_memory_dir`` on this
module (or on ``context``) still take effect because
:class:`ProjectMemoryBank` looks up the names on the module
namespace at call time — see the module-late-binding pattern in
:meth:`ProjectMemoryBank.ember_dir`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from ember_code.core.utils.context_memory_prompt import MemoryWritebackPrompt

logger = logging.getLogger(__name__)


# ── Module-level path helpers ─────────────────────────────────────
#
# Kept as module-level free functions (not class methods) because:
#
# * :mod:`context` re-exports every leading-underscore name so
#   tests can ``monkeypatch.setattr(context, "_ember_project_memory_dir",
#   ...)``. That patch reaches the ORIGINAL name here via
#   ``sys.modules[__name__]`` lookup inside the class — see
#   :meth:`ProjectMemoryBank.ember_dir`. If these lived on the class
#   directly, the monkeypatch would silently miss.
# * Tests also import them directly:
#   ``from context_memory import _ember_project_memory_dir``. The
#   module-level position preserves that.


#: Filename of the per-project memory INDEX. Individual memory files
#: are separate ``<name>.md`` files in the same directory; the index
#: is a one-line-per-memory table of contents.
_MEMORY_INDEX_NAME = "MEMORY.md"

#: Prefix line cap Claude Code publishes. Loaded into the session
#: prompt at start; the rest of the file survives untouched but is
#: not visible until the agent explicitly reads it.
_MEMORY_INDEX_MAX_LINES = 200

#: Byte cap that runs alongside the line cap — whichever hits first
#: wins. Matches Claude Code's 25 KB published cap.
_MEMORY_INDEX_MAX_BYTES = 25_000


def _project_memory_slug(project_dir: Path) -> str:
    """Encode an absolute project path as a directory-safe slug.

    Mirrors Claude Code's convention: the absolute path with every
    ``/`` replaced by ``-`` (and the leading ``/`` becomes a
    leading ``-``). So ``/Users/x/proj`` → ``-Users-x-proj``.
    Matching CC's encoding means a user who already has a CC
    memory bank for this repo automatically lights up the cross-
    tool fallback below — no migration step required.
    """
    return str(project_dir.resolve()).replace("/", "-")


def _ember_project_memory_dir(project_dir: Path) -> Path:
    """Ember-native per-project memory dir
    (``~/.ember/projects/<slug>/memory/``)."""
    return Path.home() / ".ember" / "projects" / _project_memory_slug(project_dir) / "memory"


def _claude_project_memory_dir(project_dir: Path) -> Path:
    """Claude Code's per-project memory dir
    (``~/.claude/projects/<slug>/memory/``). Read only — we never
    write here."""
    return Path.home() / ".claude" / "projects" / _project_memory_slug(project_dir) / "memory"


def _read_memory_index(memory_dir: Path) -> str:
    """Read ``MEMORY.md`` from a memory dir, applying the 200-line /
    25-KB cap. Returns ``""`` when the file doesn't exist or can't
    be read."""
    index_path = memory_dir / _MEMORY_INDEX_NAME
    try:
        if not index_path.is_file():
            return ""
        content = index_path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""
    lines = content.splitlines(keepends=True)
    if len(lines) > _MEMORY_INDEX_MAX_LINES:
        lines = lines[:_MEMORY_INDEX_MAX_LINES]
    text = "".join(lines)
    if len(text.encode("utf-8")) > _MEMORY_INDEX_MAX_BYTES:
        # Byte cap kicks in second — trim the trailing partial
        # text. ``errors="ignore"`` drops any byte chopped mid-
        # codepoint so we never emit invalid UTF-8.
        text = text.encode("utf-8")[:_MEMORY_INDEX_MAX_BYTES].decode("utf-8", errors="ignore")
    return text


# ── The bank ──────────────────────────────────────────────────────


class ProjectMemoryBank:
    """One project's auto-memory bank — the OOP core of this module.

    Owns a ``project_dir`` and a cross-tool fallback flag; exposes
    the three verbs the caller actually cares about
    (``ensure`` / ``load_index`` / ``writeback_instructions``) as
    methods. Replaces the pre-refactor free-function trio that
    threaded ``project_dir`` through five signatures as an
    implicit subject (Rule 6 "implicit subject cluster" audit
    flag).

    ## Late-binding lookup of the path helpers

    The instance methods do NOT capture the module-level
    ``_ember_project_memory_dir`` / ``_claude_project_memory_dir``
    names at method-definition time. Instead, they re-read them
    off ``sys.modules[__name__]`` at call time. That's the only
    way tests that ``monkeypatch.setattr(context_memory,
    "_ember_project_memory_dir", ...)`` — or the equivalent patch
    on the :mod:`context` re-export — take effect on subsequent
    method calls. A naive ``from … import`` at method scope would
    freeze the reference and silently ignore the monkeypatch.
    """

    #: Filename of the per-project memory INDEX — mirrors module
    #: constant so callers with a bank instance can read it off
    #: ``self.MEMORY_INDEX_NAME``.
    MEMORY_INDEX_NAME: str = _MEMORY_INDEX_NAME

    #: Prefix line cap — see module-level constant.
    MAX_LINES: int = _MEMORY_INDEX_MAX_LINES

    #: Byte cap — see module-level constant.
    MAX_BYTES: int = _MEMORY_INDEX_MAX_BYTES

    def __init__(
        self,
        project_dir: Path,
        read_claude_fallback: bool = True,
    ) -> None:
        self._project_dir = project_dir
        self._read_claude_fallback = read_claude_fallback

    # ── Path resolution ─────────────────────────────────────────

    @property
    def project_dir(self) -> Path:
        return self._project_dir

    @property
    def read_claude_fallback(self) -> bool:
        return self._read_claude_fallback

    def ember_dir(self) -> Path:
        """Resolve the ember-native memory dir for this project.

        Reads the module-level ``_ember_project_memory_dir`` name
        off ``sys.modules[__name__]`` at call time so test
        monkeypatches take effect. Do NOT inline this to
        ``_ember_project_memory_dir(self._project_dir)`` — that
        captures the reference at method-definition time and
        silently ignores patched replacements.
        """
        return sys.modules[__name__]._ember_project_memory_dir(self._project_dir)

    def claude_dir(self) -> Path:
        """Resolve the Claude Code cross-tool memory dir for this project.

        See :meth:`ember_dir` — same late-binding rationale.
        """
        return sys.modules[__name__]._claude_project_memory_dir(self._project_dir)

    # ── Verbs ───────────────────────────────────────────────────

    def ensure(self) -> Path:
        """Create the per-project memory directory if missing and
        return its path. Idempotent — existing directories are left
        alone, and any OS-level permission error is logged +
        swallowed so a flaky disk doesn't break session boot.
        """
        target = self.ember_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug("ensure_memory_dir %s failed: %s", target, exc)
        return target

    def load_index(self) -> str:
        """Load the per-project ``MEMORY.md`` index for the session.

        Tries the ember-native location first; when
        ``read_claude_fallback`` is set and no ember-native file
        exists, falls back to the equivalent CC path so a user
        mid-migration keeps their existing memory bank without
        copying files. The first 200 lines OR 25 KB (whichever cap
        hits first) load into context — the same prefix budget
        Claude Code applies — so a runaway memory file can never
        blow up the session prompt.
        """
        reader = sys.modules[__name__]._read_memory_index
        text = reader(self.ember_dir())
        if text:
            return text
        if self._read_claude_fallback:
            text = reader(self.claude_dir())
            if text:
                return text
        return ""

    def writeback_instructions(self) -> str:
        """Return the system-prompt block that teaches the agent
        how to WRITE new memory entries during a conversation
        (Claude Code parity, row 61).

        Delegates the actual template rendering to
        :class:`context_memory_prompt.MemoryWritebackPrompt` so the
        79-line prose lives in its own file (Pattern 8). This
        method's job is to wire the resolved ember-dir path into
        the prompt.
        """
        return MemoryWritebackPrompt(self.ember_dir()).render()


# ── Free-function shims ───────────────────────────────────────────
#
# One-liner delegations to :class:`ProjectMemoryBank`. Kept because:
#
# * :mod:`context` re-exports these names for backwards compat with
#   ``session/core.py`` and other external callers.
# * Every test in ``test_context.py::TestLoadMemoryIndex`` /
#   ``TestEnsureMemoryDir`` / ``TestMemoryWritebackInstructions``
#   calls them by the free-function name.
#
# Migrating every call site to the class form is out of scope for
# this file's refactor — it would ripple through 20+ tests and the
# :mod:`context` re-export block. Named as scope-limited debt.


def ensure_memory_dir(project_dir: Path) -> Path:
    """Backwards-compat shim — see :meth:`ProjectMemoryBank.ensure`."""
    return ProjectMemoryBank(project_dir).ensure()


def load_memory_index(project_dir: Path, read_claude_memory: bool = True) -> str:
    """Backwards-compat shim — see :meth:`ProjectMemoryBank.load_index`."""
    return ProjectMemoryBank(
        project_dir,
        read_claude_fallback=read_claude_memory,
    ).load_index()


def memory_writeback_instructions(project_dir: Path) -> str:
    """Backwards-compat shim — see :meth:`ProjectMemoryBank.writeback_instructions`."""
    return ProjectMemoryBank(project_dir).writeback_instructions()
