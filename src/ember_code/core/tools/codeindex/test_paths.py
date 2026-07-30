"""Test-file path detection for ``codeindex_query``.

Production code searches almost never want test items in the result
set — they're noise for "extend X" / "find existing pattern Y" /
"triage worst N" queries. :class:`TestPathClassifier` implements the
default-exclusion policy (overridable via ``include_tests=True`` on
the tool call).

Path shapes seen in the indexer:

  tests/test_foo.py                                 (Python — top-level)
  src/foo/__tests__/bar.test.ts                      (TypeScript)
  pkg/test/integration_test.go                       (Go)
  tests/test_foo.py::TestClass::test_method          (entity inside a test file)

The classifier splits on ``::`` first to isolate the file path, then
checks the file portion against the union of common-language test
conventions. Conservative on purpose: matches well-known patterns,
ignores edge cases where projects bury tests in non-conventional
folders. A dedicated module is warranted so future language-specific
toggles (Ruby's ``spec/``, Rust's ``#[cfg(test)]`` inline modules)
have a natural home.
"""

from __future__ import annotations

import re


class TestPathClassifier:
    """Classifies indexer paths as test or production.

    Stateless — the two compiled regexes are class attributes so no
    instantiation cost is incurred. Callers use the :meth:`is_test`
    classmethod directly (``TestPathClassifier.is_test(path)``) or
    hold onto an instance if future per-project rules ever need
    injection.
    """

    _DIR_RE: re.Pattern[str] = re.compile(r"(?:^|/)(?:tests?|__tests__)/", re.IGNORECASE)
    _FILE_RE: re.Pattern[str] = re.compile(
        r"(?:^|/)(?:test_[^/]+\.py|[^/]+_test\.(?:py|go)|[^/]+\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs))$",
        re.IGNORECASE,
    )

    @classmethod
    def is_test(cls, path: str | None) -> bool:
        """True iff ``path`` belongs to a test file by common conventions.

        Idempotent — works for both file paths and entity paths (which
        carry ``::`` segments; we split on ``::`` first so the regex
        only sees the file portion).
        """
        if not path:
            return False
        # Entity paths look like ``tests/test_foo.py::TestClass::test_method``.
        # Strip the entity portion so we only match against the file part.
        file_part = path.split("::", 1)[0]
        if cls._DIR_RE.search(file_part):
            return True
        return bool(cls._FILE_RE.search(file_part))
