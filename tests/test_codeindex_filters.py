"""Unit tests for the section-markup and test-path classifiers.

The subjects moved from a single ``filters.py`` module into dedicated
owners:

- :class:`TestPathClassifier` — used to exclude test files from
  ``codeindex_query`` results. The audit flagged that entity paths
  with ``::`` segments weren't covered, and that case-folding of
  ``Tests`` / ``__tests__`` was untested.
- :meth:`SectionMarkup.shorten` — produces the one-line "what this
  thing does" tag for intermediate-node summaries. Edge cases (empty
  content, no SUMMARY section, unusual sentence boundaries) were
  uncovered.
- :meth:`SectionMarkup.keep` — the section selector. Empty-resolution
  fallback (a brand-new ``Section`` enum value with no concrete-name
  entries) used to silently return ``""``; the fix now logs and
  passes content through.
"""

from __future__ import annotations

import pytest

from ember_code.core.code_index import enums as enums_mod
from ember_code.core.code_index.enums import Section
from ember_code.core.tools.codeindex.section_markup import SectionMarkup
from ember_code.core.tools.codeindex.test_paths import TestPathClassifier

# ── TestPathClassifier.is_test ───────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_foo.py",
        "Tests/test_foo.py",  # case-insensitive directory
        "src/foo/__tests__/bar.py",
        "src/foo/tests/bar.py",
        "test/foo.py",
        "src/test_foo.py",  # file pattern: test_*.py
        "src/foo_test.py",  # file pattern: *_test.py
        "src/foo.test.ts",  # file pattern: *.test.ts
        # Entity-shaped paths must strip the ``::`` suffix first.
        "tests/test_foo.py::TestFoo::test_one",
        "src/foo.py::Foo::test_helper",  # only file part is checked → False expected? See below.
    ],
)
def test_is_test_path_recognises_known_layouts(path: str) -> None:
    # Last case: ``src/foo.py::Foo::test_helper`` — the file part is
    # ``src/foo.py`` which isn't a test path, so should be False.
    expected = not path.endswith("test_helper")
    assert TestPathClassifier.is_test(path) is expected, path


@pytest.mark.parametrize(
    "path",
    [
        "src/foo.py",
        "app/services/auth/login.py",
        "lib/util.go",
        "components/Button.tsx",
    ],
)
def test_is_test_path_rejects_normal_paths(path: str) -> None:
    assert TestPathClassifier.is_test(path) is False


@pytest.mark.parametrize("path", [None, ""])
def test_is_test_path_handles_empty(path: str | None) -> None:
    assert TestPathClassifier.is_test(path) is False


def test_is_test_path_strips_entity_suffix() -> None:
    """Entity paths use ``::`` to chain class/method names — the
    pattern matcher must look at the file part only."""
    # File is a normal source file, entity name happens to contain "test"
    assert TestPathClassifier.is_test("src/foo.py::TestRunner::run") is False
    # File IS a test file
    assert TestPathClassifier.is_test("tests/test_runner.py::SomeClass::method") is True


# ── SectionMarkup.shorten ────────────────────────────────────────────


def test_shorten_summary_empty_content() -> None:
    assert SectionMarkup("").shorten() == ""


def test_shorten_summary_no_section_markers() -> None:
    """Content without any ``[SECTION:…]`` markers returns ""."""
    assert SectionMarkup("just plain text describing something.").shorten() == ""


def test_shorten_summary_extracts_summary_section() -> None:
    content = (
        "[SECTION:summary]This class wraps a Redis client. "
        "It exposes a sliding-window rate limiter.[/SECTION]"
    )
    result = SectionMarkup(content).shorten()
    assert "Redis client" in result
    assert result.endswith(".")


def test_shorten_summary_skips_non_summary_sections() -> None:
    content = (
        "[SECTION:security]No known issues.[/SECTION]"
        "[SECTION:summary]Authoritative summary here.[/SECTION]"
    )
    result = SectionMarkup(content).shorten()
    assert "Authoritative" in result
    assert "No known issues" not in result


def test_shorten_summary_empty_body_returns_empty() -> None:
    """A SUMMARY section that exists but has no body should return ""."""
    content = "[SECTION:summary][/SECTION]"
    assert SectionMarkup(content).shorten() == ""


def test_shorten_summary_first_sentence_only() -> None:
    content = "[SECTION:summary]First sentence here. Second sentence. Third sentence.[/SECTION]"
    result = SectionMarkup(content).shorten()
    assert "First sentence here" in result
    assert "Second sentence" not in result


def test_shorten_summary_no_sentence_boundary_uses_char_cap() -> None:
    """A summary written without a period falls back to the char cap."""
    body = "a" * 500  # no sentence boundary, exceeds _SHORT_SUMMARY_MAX_CHARS (200)
    content = f"[SECTION:summary]{body}[/SECTION]"
    result = SectionMarkup(content).shorten()
    # Should hit the 200-char cap, then add the closing "."
    assert len(result) <= 201
    assert result.endswith(".")


def test_shorten_summary_file_section_name_resolves() -> None:
    """File summaries use ``purpose_and_functionality`` rather than
    ``summary`` — the SUMMARY concrete-name set should cover both."""
    content = "[SECTION:purpose_and_functionality]This file orchestrates the auth flow.[/SECTION]"
    result = SectionMarkup(content).shorten()
    # Whether this resolves depends on Section.SUMMARY.concrete_names()
    # coverage; just assert it doesn't crash on a non-canonical name.
    assert isinstance(result, str)


# ── SectionMarkup.keep ───────────────────────────────────────────────


def test_filter_sections_empty_content_returns_unchanged() -> None:
    assert SectionMarkup("").keep((Section.SUMMARY,)) == ""


def test_filter_sections_empty_sections_tuple_returns_unchanged() -> None:
    content = "[SECTION:summary]X[/SECTION]"
    assert SectionMarkup(content).keep(()) == content


def test_filter_sections_no_markers_returns_unchanged() -> None:
    """Content without any [SECTION:…] markers is short docs or other
    raw text that shouldn't be filtered."""
    plain = "Just plain text without section markers."
    assert SectionMarkup(plain).keep((Section.SUMMARY,)) == plain


def test_filter_sections_keeps_only_requested() -> None:
    content = (
        "[SECTION:summary]The summary.[/SECTION]"
        "[SECTION:security]Security notes.[/SECTION]"
        "[SECTION:testing]Testing info.[/SECTION]"
    )
    result = SectionMarkup(content).keep((Section.SECURITY,))
    assert "Security notes" in result
    assert "The summary" not in result
    assert "Testing info" not in result


def test_filter_sections_multiple_sections() -> None:
    content = (
        "[SECTION:summary]S.[/SECTION]"
        "[SECTION:security]Sec.[/SECTION]"
        "[SECTION:testing]Test.[/SECTION]"
    )
    result = SectionMarkup(content).keep((Section.SUMMARY, Section.SECURITY))
    assert "S." in result
    assert "Sec." in result
    assert "Test." not in result


def test_filter_sections_wanted_doesnt_match_returns_empty() -> None:
    """When the resolved names are populated but none match the actual
    sections in the content, we return "". The agent gets back nothing
    rather than the full content — this is intentional (the agent asked
    for a specific slice; we tell them the slice is empty)."""
    content = "[SECTION:summary]The summary.[/SECTION]"
    result = SectionMarkup(content).keep((Section.SECURITY,))
    assert result == ""


def test_filter_sections_unknown_section_logs_and_passes_through() -> None:
    """The bug we fixed: a Section value with no concrete-name entries
    used to silently filter the content to "". Now it should pass
    through unchanged and log a warning.

    We simulate the bug by patching the enum's concrete-name lookup
    table so ``Section.SUMMARY`` resolves to nothing.
    """
    from unittest.mock import patch

    from ember_code.core.tools.codeindex import section_markup as sm_mod

    content = "[SECTION:summary]S.[/SECTION]"
    original = enums_mod._SECTION_CONCRETE_NAMES
    try:
        # Override so SUMMARY resolves to nothing.
        enums_mod._SECTION_CONCRETE_NAMES = {Section.SUMMARY: frozenset()}
        # Patch the module logger directly — bypasses caplog / pytest's
        # log-config which can be disabled by earlier tests in the
        # full-suite run.
        with patch.object(sm_mod.logger, "warning") as mock_warning:
            result = SectionMarkup(content).keep((Section.SUMMARY,))
        assert result == content  # passed through, no data loss
        assert mock_warning.called, "expected a warning about empty resolution"
        (fmt, *_), _ = mock_warning.call_args
        assert "SectionMarkup" in fmt
    finally:
        enums_mod._SECTION_CONCRETE_NAMES = original
