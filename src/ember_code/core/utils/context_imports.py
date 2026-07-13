"""Claude-Code-style ``@<path>.md`` import resolution for rules files.

Extracted from :mod:`ember_code.core.utils.context` per
CODE_STANDARDS.md Pattern 8. Pure module — no config, no globals,
no I/O except reading the imported file itself.

## What lives here

- Regex constants for detecting ``@<path>`` tokens, fenced code
  blocks, and inline code spans.
- :func:`resolve_at_path` — translate ``@<token>`` into an absolute
  path under an ``allowed_root``. Returns ``None`` when the token
  points outside the root or at a missing file.
- :func:`mask_code_regions` / :func:`unmask_code_regions` — protect
  code fences + inline code from accidental ``@`` substitution.
  Rules files often document ``@<path>.md`` syntax; we don't want
  those examples to trigger imports.
- :func:`resolve_imports` — the main recursive resolver. Cycle-safe,
  depth-capped, code-region-aware.

## Depth cap

``IMPORT_MAX_DEPTH = 4`` matches Claude Code's published limit.
Enough for the natural ``CLAUDE.md → @conventions.md →
@style.md → @detail.md`` chain but tight enough that accidental
fan-outs or cycles can't blow up the prompt. Adjust in lockstep
with the depth-cap test in ``test_context.py``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Regex constants ─────────────────────────────────────────────────

#: ``@<path>.md`` — non-whitespace, non-``)`` (so ``[label](./foo.md)``
#: isn't chewed up), ending in ``.md``.
AT_IMPORT_RE = re.compile(r"@([^\s)]+\.md)")

#: How many nested ``@<path>`` levels to follow before giving up.
#: Matches Claude Code's documented limit.
IMPORT_MAX_DEPTH = 4

#: Fenced code blocks — ``` or ~~~ runs (≥3 chars), optionally
#: indented up to 3 spaces, with a matching closing fence on its
#: own line. Multiline + DOTALL so the body can span lines.
FENCED_BLOCK_RE = re.compile(
    r"^[ ]{0,3}(```+|~~~+)[^\n]*\n"
    r".*?"
    r"(?:^[ ]{0,3}\1[ \t]*(?:\n|$))",
    re.MULTILINE | re.DOTALL,
)

#: Inline code spans — backtick-delimited, single line. Permissive
#: on fence length; over-masking is harmless.
INLINE_CODE_RE = re.compile(r"`+[^`\n]*`+")

#: Sentinel marker used while masking. NUL bytes don't appear in
#: normal text so the placeholder is unambiguous.
CODE_SENTINEL_RE = re.compile(r"\0CODE(\d+)\0")


# ── Resolution primitives ───────────────────────────────────────────


def resolve_at_path(token: str, source_path: Path, allowed_root: Path) -> Path | None:
    """Translate one ``@<token>`` into an absolute path under
    ``allowed_root``. Returns ``None`` when the token doesn't
    resolve to an existing file inside the root — the caller
    leaves the literal token in place in that case.

    Token forms:
    - ``@~/rules.md`` — home-relative
    - ``@/etc/rules.md`` — absolute
    - ``@./rel.md`` or ``@rel.md`` — relative to ``source_path``'s
      directory

    Any token pointing outside ``allowed_root`` (via ``..``,
    absolute paths outside the root, or symlink escape) yields
    ``None`` — the sandboxing invariant that keeps a managed
    policy from reaching into ``/etc/passwd``.
    """
    try:
        if token.startswith("~"):
            candidate = Path(token).expanduser()
        elif token.startswith("/"):
            candidate = Path(token)
        else:
            candidate = source_path.parent / token
        candidate = candidate.resolve()
        candidate.relative_to(allowed_root.resolve())
    except (OSError, ValueError):
        return None
    if not candidate.is_file():
        return None
    return candidate


def mask_code_regions(content: str) -> tuple[str, list[str]]:
    """Replace fenced code blocks and inline code spans with NUL-
    delimited sentinels so a later ``@`` substitution pass doesn't
    touch their contents. Returns ``(masked, originals)`` where
    ``originals[i]`` is the substring replaced by sentinel ``i``.

    Fenced blocks are masked first (multiline), then inline
    code spans on whatever's left. Over-masking is acceptable —
    the only effect is leaving ``@`` tokens inside the masked
    region as literals, which is exactly what we want for code.
    """
    originals: list[str] = []

    def stash(m: re.Match[str]) -> str:
        idx = len(originals)
        originals.append(m.group(0))
        return f"\0CODE{idx}\0"

    masked = FENCED_BLOCK_RE.sub(stash, content)
    masked = INLINE_CODE_RE.sub(stash, masked)
    return masked, originals


def unmask_code_regions(content: str, originals: list[str]) -> str:
    """Reverse of :func:`mask_code_regions` — restore stashed code
    regions identified by the ``\\0CODE<idx>\\0`` sentinels."""

    def restore(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(originals):
            return originals[idx]
        return m.group(0)

    return CODE_SENTINEL_RE.sub(restore, content)


def resolve_imports(
    content: str,
    source_path: Path,
    allowed_root: Path,
    seen: set[Path] | None = None,
    depth: int = 0,
) -> str:
    """Inline ``@<path>.md`` imports in ``content``.

    Recursive, capped at :data:`IMPORT_MAX_DEPTH`. Cycle-safe via
    ``seen`` — once a file has been inlined in this resolution
    chain, a later ``@`` referencing it leaves the literal token
    so the agent can see the unresolved reference instead of
    looping. Imports escaping ``allowed_root`` are also left as
    literals — see :func:`resolve_at_path`.

    Tokens inside code spans (`` `…` ``) and fenced code blocks
    (```` ``` ```` / ``~~~``) are deliberately NOT inlined — they're
    masked out before the substitution pass and restored after,
    so rules files can document ``@<path>.md`` syntax without
    triggering accidental imports.
    """
    if depth >= IMPORT_MAX_DEPTH:
        return content
    if seen is None:
        seen = set()

    def replacer(m: re.Match[str]) -> str:
        token = m.group(1)
        resolved = resolve_at_path(token, source_path, allowed_root)
        if resolved is None or resolved in seen:
            return m.group(0)
        try:
            inner = resolved.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("@ import read %s failed: %s", resolved, exc)
            return m.group(0)
        seen.add(resolved)
        return resolve_imports(inner, resolved, allowed_root, seen, depth + 1)

    masked, originals = mask_code_regions(content)
    substituted = AT_IMPORT_RE.sub(replacer, masked)
    return unmask_code_regions(substituted, originals)
