"""Tests for :mod:`ember_code.core.utils.context_frontmatter` —
the extracted YAML-frontmatter parser + path-glob matcher.

The parent-module tests (test_context.py) already exercise the
frontmatter behaviour via the higher-level loaders. These tests
lock in the extracted primitives directly so a future refactor of
the parent can't silently break the extracted contract.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.utils.context_frontmatter import (
    matches_paths,
    parse_frontmatter,
)


class TestParseFrontmatter:
    def test_no_frontmatter_returns_empty_paths(self):
        paths, body = parse_frontmatter("just a body\nno frontmatter")
        assert paths == []
        assert body == "just a body\nno frontmatter"

    def test_frontmatter_without_paths_returns_empty(self):
        content = "---\nname: rule\n---\nbody"
        paths, body = parse_frontmatter(content)
        assert paths == []
        assert body == "body"

    def test_inline_list_form(self):
        content = '---\npaths: ["docs/**", "tests/**"]\n---\nbody'
        paths, body = parse_frontmatter(content)
        assert paths == ["docs/**", "tests/**"]
        assert body == "body"

    def test_block_list_form(self):
        content = "---\npaths:\n  - docs/**\n  - tests/**\n---\nbody"
        paths, body = parse_frontmatter(content)
        assert paths == ["docs/**", "tests/**"]
        assert body == "body"

    def test_block_list_stops_at_next_key(self):
        # Regression: prior implementations kept consuming lines
        # past ``paths:`` if they didn't start with ``- ``. Confirm
        # a following key ends the block.
        content = "---\npaths:\n  - docs/**\nname: rule\n  - not-a-path\n---\nbody"
        paths, _body = parse_frontmatter(content)
        # Second entry after ``name:`` must NOT be treated as a path.
        assert paths == ["docs/**"]

    def test_quoted_values_are_unwrapped(self):
        # Both single and double quotes are stripped so YAML users
        # can quote their globs when they contain special chars.
        content = "---\npaths:\n  - \"docs/**\"\n  - 'tests/**'\n---\nbody"
        paths, _body = parse_frontmatter(content)
        assert paths == ["docs/**", "tests/**"]

    def test_empty_paths_list(self):
        content = "---\npaths: []\n---\nbody"
        paths, _body = parse_frontmatter(content)
        assert paths == []

    def test_body_is_everything_after_frontmatter(self):
        content = "---\npaths: []\n---\nfirst line\nsecond line"
        _paths, body = parse_frontmatter(content)
        assert body == "first line\nsecond line"


class TestMatchesPaths:
    def test_no_paths_always_matches(self):
        # An empty ``paths:`` list means the rule applies unconditionally.
        assert matches_paths([], Path("/anywhere"), None) is True

    def test_no_working_dir_never_matches_scoped_rule(self):
        # A scoped rule can't match without a candidate to check
        # against — conservative default is "doesn't apply".
        assert matches_paths(["docs/**"], None, None) is False

    def test_matches_absolute_path(self):
        assert matches_paths(["/Users/*/proj/**"], Path("/Users/x/proj/main.py"), None) is True

    def test_matches_project_relative_path(self, tmp_path):
        # The matcher tries BOTH absolute and project-relative
        # candidates. Project-relative globs are the common form.
        working = tmp_path / "docs" / "guide.md"
        assert matches_paths(["docs/*"], working, tmp_path) is True

    def test_no_match_returns_false(self, tmp_path):
        working = tmp_path / "src" / "foo.py"
        assert matches_paths(["docs/**"], working, tmp_path) is False

    def test_multiple_globs_any_match_wins(self, tmp_path):
        working = tmp_path / "tests" / "test_foo.py"
        assert matches_paths(["docs/**", "tests/**"], working, tmp_path) is True

    def test_working_dir_outside_project_still_checks_absolute(self, tmp_path):
        # A working_dir outside the project can still match an
        # absolute glob — the ``relative_to`` fallback is suppressed
        # via contextlib.suppress and the absolute candidate still
        # gets tested. Use a real absolute path derived from
        # ``tmp_path`` (guaranteed to resolve without symlinks) so
        # the fnmatch input is stable across OSes.
        working = tmp_path / "outer.py"
        outside_project = tmp_path.parent / "not_a_project"
        # Absolute glob covers `tmp_path`'s tree.
        pattern = f"{tmp_path}/**"
        assert matches_paths([pattern], working, outside_project) is True
