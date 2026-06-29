"""Tests for ``protocol/agno_events._format_edit_diff`` —
specifically the third element of its return tuple (``rows``),
which is pure ``(display_text, style_string)`` data testable
without rendering Rich tables.

The function takes a tool with ``old_string`` / ``new_string`` /
optional ``file_path`` args, runs ``difflib.SequenceMatcher``
on the two strings, and emits one row per output line with:

  * ``-`` prefix + red-on-dark-red background for deletes
  * ``+`` prefix + green-on-dark-green background for inserts
  * ``  `` (two-space) prefix + empty style for unchanged
  * 4-char-right-aligned line numbers
  * ``start_line`` from the file's content if ``file_path``
    resolves and the new content can be located in it; else 1

These styling + numbering details show up on every edit-tool
card; drift would silently degrade the diff display.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ember_code.protocol.agno_events import _format_edit_diff

# Colour codes the source uses. Pinned here so a future palette
# refactor must update BOTH places (deliberate, not silent).
DELETE_STYLE = "#ff6b6b on #3d0000"
INSERT_STYLE = "#69db7c on #003d00"


def _tool(args: dict[str, Any] | None) -> Any:
    return SimpleNamespace(tool_args=args)


def _rows(args: dict[str, Any] | None) -> list[tuple[str, str]] | None:
    result = _format_edit_diff(_tool(args))
    if result is None:
        return None
    return result[2]


class TestSentinels:
    def test_none_args_returns_none(self):
        # The tool may be in mid-construction or have been
        # malformed; defensive None return.
        assert _format_edit_diff(_tool(None)) is None

    def test_non_dict_args_returns_none(self):
        # ``tool_args`` could be any shape from Agno; only
        # dict-shaped args carry the diff fields.
        assert _format_edit_diff(_tool("garbage")) is None  # type: ignore[arg-type]

    def test_both_empty_returns_none(self):
        # No diff to render — don't spawn an empty card.
        assert _format_edit_diff(_tool({"old_string": "", "new_string": ""})) is None

    def test_no_diff_lines_returns_none(self):
        # Equal old and new with no SequenceMatcher rows
        # (empty match runs) — the function returns None
        # rather than an empty rows list.
        result = _format_edit_diff(
            _tool({"old_string": "", "new_string": ""}),
        )
        assert result is None


class TestDeleteOnly:
    def test_delete_one_line(self):
        rows = _rows({"old_string": "removed line", "new_string": ""})
        assert rows is not None
        assert len(rows) == 1
        text, style = rows[0]
        assert style == DELETE_STYLE
        assert text.startswith("-")
        # Line number is right-aligned in a 4-char field.
        assert "   1" in text  # ``   1`` (4-char right-pad of "1")
        assert "removed line" in text

    def test_delete_multiple_lines(self):
        rows = _rows({"old_string": "a\nb\nc", "new_string": ""})
        assert rows is not None
        assert len(rows) == 3
        for r in rows:
            assert r[1] == DELETE_STYLE


class TestInsertOnly:
    def test_insert_one_line(self):
        rows = _rows({"old_string": "", "new_string": "added line"})
        assert rows is not None
        assert len(rows) == 1
        text, style = rows[0]
        assert style == INSERT_STYLE
        assert text.startswith("+")
        assert "added line" in text

    def test_insert_multiple_lines(self):
        rows = _rows({"old_string": "", "new_string": "a\nb\nc"})
        assert rows is not None
        assert len(rows) == 3
        for r in rows:
            assert r[1] == INSERT_STYLE


class TestReplaceEmitsDeleteThenInsert:
    """The ``replace`` opcode from difflib is the most common
    case — a line edited in place. The function emits ALL the
    deletes first, then ALL the inserts (diff convention)."""

    def test_simple_replace(self):
        rows = _rows({"old_string": "before", "new_string": "after"})
        assert rows is not None
        # 1 delete + 1 insert.
        assert len(rows) == 2
        assert rows[0][1] == DELETE_STYLE
        assert "before" in rows[0][0]
        assert rows[1][1] == INSERT_STYLE
        assert "after" in rows[1][0]

    def test_multi_line_replace(self):
        # 2-line replace: 2 deletes, then 2 inserts. NOT
        # interleaved — diff convention.
        rows = _rows({"old_string": "a\nb", "new_string": "c\nd"})
        assert rows is not None
        styles = [r[1] for r in rows]
        # Two deletes followed by two inserts.
        assert styles == [DELETE_STYLE, DELETE_STYLE, INSERT_STYLE, INSERT_STYLE]


class TestEqualLinesUnstyled:
    def test_equal_lines_pass_through_with_empty_style(self):
        # Lines present in both old and new render unstyled
        # (no background colour). The 2-space "  " prefix
        # marks them as context.
        rows = _rows({"old_string": "x\nedit\nz", "new_string": "x\nnew\nz"})
        assert rows is not None
        # Find the "x" and "z" lines (equal context).
        x_row = next(r for r in rows if "x" in r[0] and r[0].startswith("  "))
        z_row = next(r for r in rows if "z" in r[0] and r[0].startswith("  "))
        assert x_row[1] == ""
        assert z_row[1] == ""

    def test_unchanged_uses_two_space_prefix(self):
        # The convention: ``-`` for delete, ``+`` for insert,
        # ``  `` (two spaces) for unchanged. The two-space
        # alignment lets the line numbers + content sit at
        # the same column regardless of diff state.
        rows = _rows({"old_string": "same", "new_string": "same"})
        # No diff at all → None (covered above), but if equal
        # lines appear AROUND a diff, they have the 2-space
        # prefix.
        rows = _rows({"old_string": "same\nold", "new_string": "same\nnew"})
        assert rows is not None
        same_row = next(r for r in rows if "same" in r[0])
        assert same_row[0].startswith("  ")


class TestLineNumberFormat:
    def test_line_numbers_right_aligned_in_4char_field(self):
        # The ``{n:>4}`` formatter pads to width 4. Pin so a
        # future refactor that changes the width is a
        # deliberate choice (would mis-align with the prefix).
        rows = _rows({"old_string": "", "new_string": "x"})
        assert rows is not None
        # ``+ ``  (prefix + space) + ``   1`` (right-padded) +
        # ``   `` (separator) + content.
        # Pin the right-pad: a 4-digit line number should fit
        # snugly with no extra space.
        rows4 = _rows({"old_string": "", "new_string": "\n".join(str(i) for i in range(1, 5))})
        assert rows4 is not None and len(rows4) == 4
        # Find the row for line 4 — should be ``+    4   4``.
        # ``{4:>4}`` is ``"   4"`` (3 leading spaces).
        line4 = rows4[3][0]
        # Check that "   4" appears (the right-padded line
        # number, NOT just "4").
        assert "   4" in line4

    def test_line_numbers_for_inserts_start_at_1_by_default(self):
        # No file_path → start_line=1. The line numbers count
        # within the inserted block.
        rows = _rows({"old_string": "", "new_string": "a\nb\nc"})
        assert rows is not None
        # The three inserted lines should be numbered 1, 2, 3.
        assert "   1" in rows[0][0]
        assert "   2" in rows[1][0]
        assert "   3" in rows[2][0]


class TestStartLineFromFile:
    def test_start_line_detected_from_file_path(self, tmp_path):
        # The source searches for ``new_string`` in the file
        # — works when the file ALREADY contains the new
        # content (e.g. rendering history after the edit
        # landed). For live in-flight edits, the file still
        # has the old content and ``find`` returns -1 → line 1.
        # This test simulates the post-edit state where
        # ``new_string`` is in the file at line 5.
        f = tmp_path / "src.py"
        f.write_text(
            "def foo():\n    pass\n\ndef bar():\n    return 2\n",
        )
        # ``new_string`` ("    return 2") is in the file at
        # line 5 — find() returns its offset and the line
        # numbers in the diff start there.
        rows = _rows(
            {
                "old_string": "    return 1",
                "new_string": "    return 2",
                "file_path": str(f),
            },
        )
        assert rows is not None
        # Both delete + insert reference line 5.
        for r in rows:
            assert "   5" in r[0]

    def test_live_edit_finds_line_via_old_string_fallback(self, tmp_path):
        # Live in-flight edit: the file still contains
        # ``old_string`` (the edit hasn't landed yet) and NOT
        # ``new_string``. The source falls back to searching
        # for ``old_string`` so live edit cards still show
        # real line numbers rather than always "line 1".
        # Previously this case fell back to line 1 — bug
        # surfaced + fixed by writing this test (the prior
        # iteration's pinning of "line 1" revealed the
        # behavior was wrong, not just quirky).
        f = tmp_path / "src.py"
        f.write_text(
            "def foo():\n    pass\n\ndef bar():\n    return 1\n",
        )
        rows = _rows(
            {
                "old_string": "    return 1",  # in file at line 5
                "new_string": "    return 2",  # NOT in file yet
                "file_path": str(f),
            },
        )
        assert rows is not None
        # All rows reference the actual source line (5), not 1.
        for r in rows:
            assert "   5" in r[0]

    def test_neither_in_file_falls_back_to_line_1(self, tmp_path):
        # When NEITHER new_string nor old_string can be found
        # (file completely rewritten between read and edit
        # — rare race), start_line stays at 1. Documented
        # last-resort fallback.
        f = tmp_path / "x.py"
        f.write_text("totally unrelated content\n")
        rows = _rows(
            {
                "old_string": "not-in-file-either",
                "new_string": "definitely-not-in-file",
                "file_path": str(f),
            },
        )
        assert rows is not None
        for r in rows:
            assert "   1" in r[0]

    def test_missing_file_falls_back_to_line_1(self, tmp_path):
        # File doesn't exist — except is caught silently in
        # the source, start_line stays at 1.
        rows = _rows(
            {
                "old_string": "old",
                "new_string": "new",
                "file_path": str(tmp_path / "does_not_exist.py"),
            },
        )
        assert rows is not None
        # Falls back to 1.
        for r in rows:
            assert "   1" in r[0]

    def test_new_string_not_in_file_falls_back_to_line_1(self, tmp_path):
        # File exists but new_string isn't in it (unusual but
        # possible — file changed between read and edit). The
        # find returns -1 and start_line stays at 1.
        f = tmp_path / "x.py"
        f.write_text("totally different content\n")
        rows = _rows(
            {
                "old_string": "old",
                "new_string": "definitely-not-in-file",
                "file_path": str(f),
            },
        )
        assert rows is not None
        for r in rows:
            assert "   1" in r[0]
