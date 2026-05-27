"""Section filtering, where-clause building, and shared constants.

These are the value-only helpers that the codeindex services lean on:

  - :data:`SECTION_ALIASES` / :data:`SECTION_RE` — the indexer writes
    ``[SECTION:<name>]…[/SECTION]`` blocks; this module owns the regex
    and the alias map that resolves a semantic group (``Section.SECURITY``)
    to the concrete section names used at file/entity/folder level.
  - :func:`shorten_summary`, :func:`filter_sections` — the two text
    operations that touch section blocks.
  - :func:`build_where` — translate a :class:`_CategoricalFilters` into
    a chroma metadata ``where`` filter.
  - Disambiguation knobs (``DISAMBIGUATION_TOP_N``,
    ``DISAMBIGUATION_REFS_PER_DIRECTION``) — collocated here because
    they're tunable constants the services consume.

Everything here is stateless — pure functions and module-level
constants. Service classes import what they need; the toolkit doesn't
touch this module directly.
"""

from __future__ import annotations

import re
from typing import Any

from ember_code.core.code_index.enums import Section
from ember_code.core.tools.codeindex.schemas import (
    _CATEGORICAL_QUALITY_FIELDS,
    _CategoricalFilters,
)

# ── Section regexes / aliases ────────────────────────────────────────
#
# The indexer's LLM-summary pass writes ``content`` as
# ``[SECTION:<name>]…[/SECTION]`` blocks, but the concrete section
# names differ per item type (file / entity / folder). Agents pick
# semantic groups via the ``Section`` enum on ``codeindex_query``;
# this map expands each group to the set of concrete names that
# carry that meaning across item types.

SECTION_ALIASES: dict[Section, frozenset[str]] = {
    Section.SUMMARY: frozenset({"summary", "purpose_and_functionality", "module_purpose"}),
    Section.QUALITY: frozenset({"quality_assessment", "code_quality", "quality_patterns"}),
    Section.SECURITY: frozenset({"security_analysis", "security", "security_posture"}),
    Section.ISSUES: frozenset(
        {"issues_and_concerns", "issues_and_technical_debt", "common_issues"}
    ),
    Section.TESTING: frozenset({"testing_status", "testing_and_reliability"}),
    Section.ARCHITECTURE: frozenset(
        {"architecture_and_design", "organization_and_structure", "architectural_assessment"}
    ),
    Section.DEPENDENCIES: frozenset({"dependencies_and_impact"}),
    Section.RECOMMENDATIONS: frozenset({"recommendations"}),
    Section.HEALTH_SCORE: frozenset({"module_health_score"}),
    Section.ENTITIES: frozenset({"entities"}),
}

SECTION_RE = re.compile(
    r"\[SECTION:(?P<name>[a-z_]+)\](?P<body>.*?)\[/SECTION\]",
    re.DOTALL,
)
DEFAULT_SECTIONS: tuple[Section, ...] = (Section.SUMMARY,)

_SUMMARY_NAMES: frozenset[str] = SECTION_ALIASES[Section.SUMMARY]
_SHORT_SUMMARY_MAX_CHARS = 200


# ── Test-path detection ──────────────────────────────────────────────
#
# Production code searches almost never want test items in the result
# set — they're noise for "extend X" / "find existing pattern Y" /
# "triage worst N" queries. Excluding tests by default cleans up the
# top-K and frees ranking slots for actual production code.
#
# Path shapes seen in the indexer:
#   tests/test_foo.py                                 (Python — top-level)
#   src/foo/__tests__/bar.test.ts                      (TypeScript)
#   pkg/test/integration_test.go                       (Go)
#   tests/test_foo.py::TestClass::test_method          (entity inside a test file)
#
# We split on ``::`` first to isolate the file path, then check the
# file portion against the union of common-language test conventions.
# Conservative on purpose: matches well-known patterns, ignores edge
# cases where projects bury tests in non-conventional folders.

_TEST_DIR_PATTERN = re.compile(r"(?:^|/)(?:tests?|__tests__)/", re.IGNORECASE)
_TEST_FILE_PATTERN = re.compile(
    r"(?:^|/)(?:test_[^/]+\.py|[^/]+_test\.(?:py|go)|[^/]+\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs))$",
    re.IGNORECASE,
)


def is_test_path(path: str | None) -> bool:
    """True iff ``path`` belongs to a test file by common conventions.

    Used by ``codeindex_query`` to filter out test items by default
    (overridable via ``include_tests=True``). Idempotent — works for
    both file paths and entity paths (which carry ``::`` segments).
    """
    if not path:
        return False
    # Entity paths look like ``tests/test_foo.py::TestClass::test_method``.
    # Strip the entity portion so we only match against the file part.
    file_part = path.split("::", 1)[0]
    if _TEST_DIR_PATTERN.search(file_part):
        return True
    return bool(_TEST_FILE_PATTERN.search(file_part))


# ── Disambiguation-refs knobs (codeindex_query path) ─────────────────
#
# When a ``query_text`` search returns multiple items, we surface
# disambiguating reference data for the top-N most-relevant items.
# For each, we fetch the entity's callers + callees from sqlite,
# re-score each reference's summary against the SAME ``query_text``
# (so the agent sees how relevant each user / dependency is to its
# original intent), and keep the top-K of each direction.
#
# Why these defaults:
#   - Top-10 items: chroma's top-3 often contains files / module-level
#     constants / unrelated near-miss methods. The actual entities the
#     agent needs to disambiguate against frequently land at ranks
#     6–12 in semantically dense areas (rate-limiting, retries, auth).
#     Ten covers that band; the per-item ref fetch is a single batched
#     sqlite call so the cost stays flat in N.
#   - Top-10 refs per direction: enough to show a usage signature
#     (e.g. "all callers are API endpoints" vs "all callers are
#     streaming workers") without bloating the response.
DISAMBIGUATION_TOP_N: int = 10
DISAMBIGUATION_REFS_PER_DIRECTION: int = 10


# ── Section filtering ────────────────────────────────────────────────


def shorten_summary(content: str) -> str:
    """Extract the first SUMMARY-group section from ``content`` and
    return its first sentence (or first ``_SHORT_SUMMARY_MAX_CHARS``
    chars, whichever is shorter). Used to give the agent a one-line
    "what this thing does" alongside reference edges. Returns "" if
    the content has no markers or no summary section.
    """
    if not content:
        return ""
    for m in SECTION_RE.finditer(content):
        if m.group("name") not in _SUMMARY_NAMES:
            continue
        body = m.group("body").strip()
        if not body:
            return ""
        # Take the first sentence — most LLM-generated summaries open
        # with a one-sentence "this does X" before elaborating.
        first_sentence, _, _ = body.partition(". ")
        first_sentence = first_sentence.strip().rstrip(".")
        # Fall back to a hard char cap so a summary written without
        # sentence boundaries still fits.
        if not first_sentence or len(first_sentence) > _SHORT_SUMMARY_MAX_CHARS:
            first_sentence = body[:_SHORT_SUMMARY_MAX_CHARS].rstrip()
        return f"{first_sentence}."
    return ""


def filter_sections(content: str, sections: tuple[Section, ...]) -> str:
    """Keep only the requested ``[SECTION:…]…[/SECTION]`` blocks.

    ``sections`` carries semantic groups (e.g. ``Section.SECURITY``);
    the alias map resolves each group to the concrete section names
    used at file / entity / folder level. Returns the joined matching
    blocks (newline-separated) in the order they appear in the
    original content. If the content has no section markers, returns
    it unchanged — short docs / non-summarized items don't get
    filtered. If the resolved name set doesn't match anything in the
    content, returns an empty string (agent gets back what's actually
    there, which may be nothing).
    """
    if not content or not sections:
        return content
    wanted: set[str] = set()
    for s in sections:
        wanted |= SECTION_ALIASES.get(s, frozenset())
    # If every requested Section value resolved to an empty alias set,
    # the caller hit a gap in ``SECTION_ALIASES`` (typically a new
    # Section enum member added without updating the alias map). Pass
    # the content through unchanged and warn so the gap surfaces —
    # silently returning "" used to make whole entity summaries
    # disappear at the agent's eyes for what is purely an internal
    # configuration bug.
    if not wanted:
        import logging
        logging.getLogger(__name__).warning(
            "filter_sections: no concrete names resolved for %r — "
            "check SECTION_ALIASES coverage. Passing content through.",
            sections,
        )
        return content
    matches = list(SECTION_RE.finditer(content))
    if not matches:
        return content
    kept = [
        f"[SECTION:{m.group('name')}]{m.group('body')}[/SECTION]"
        for m in matches
        if m.group("name") in wanted
    ]
    return "\n\n".join(kept)


# ── Where-clause builder ─────────────────────────────────────────────


def _enum_value(v: Any) -> Any:
    """StrEnum → raw string so chroma sees what it stored."""
    if hasattr(v, "value"):
        return v.value
    return v


def build_where(filters: _CategoricalFilters) -> dict[str, Any] | None:
    """Translate :class:`_CategoricalFilters` into a chroma ``where`` filter.

    Every non-None field becomes one clause; multiple clauses combine
    under a top-level ``$and``. Single values become direct equality,
    lists become ``$in``.

    List-shaped multi-value categories live on :class:`_ListFilters`
    and are applied Python-side — chroma metadata ``where`` lacks a
    ``$contains`` operator, so they can't be pushed down here.

    Returns ``None`` when no filters were supplied so the index code
    skips the where-clause entirely (chroma rejects ``where={}``).
    """
    clauses: list[dict[str, Any]] = []

    # Direct exact-match scope filters.
    if filters.kind is not None:
        clauses.append({"kind": _enum_value(filters.kind)})
    if filters.type is not None:
        clauses.append({"type": filters.type})
    if filters.file_extension is not None:
        clauses.append({"file_extension": filters.file_extension})
    # ``path_prefix`` is reserved — chroma metadata where has no
    # $contains/prefix operator, so we accept the arg and ignore it
    # rather than silently emit a broken filter. Re-enable once
    # there's a where-document-based path matcher.

    # ``entity_type`` — single value or list.
    if filters.entity_type is not None:
        if isinstance(filters.entity_type, list):
            clauses.append({"entity_type": {"$in": [str(x) for x in filters.entity_type]}})
        else:
            clauses.append({"entity_type": str(filters.entity_type)})

    # ``needs_refactoring`` is bool.
    if filters.needs_refactoring is not None:
        clauses.append({"needs_refactoring": bool(filters.needs_refactoring)})

    # Quality categoricals.
    for field in _CATEGORICAL_QUALITY_FIELDS:
        v = getattr(filters, field)
        if v is None:
            continue
        if isinstance(v, list):
            values = [_enum_value(x) for x in v if x is not None]
            if not values:
                continue
            if len(values) == 1:
                clauses.append({field: values[0]})
            else:
                clauses.append({field: {"$in": values}})
        else:
            clauses.append({field: _enum_value(v)})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}
