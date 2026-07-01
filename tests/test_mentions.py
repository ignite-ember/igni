"""Tests for ``core/utils/mentions.process_file_mentions``.

This helper runs on every user message — picks out ``@file``
mentions, surfaces them in the ``<attached-files>`` hint block
the agent reads, and (despite the docstring's claim) preserves
the literal ``@<path>`` tokens in the body so the rendered
bubble shows what the user typed.

Subtle invariants worth pinning:

  * Email-style ``user@domain`` must NOT trigger a mention (the
    regex requires ``@`` to be preceded by start-of-string or
    whitespace).
  * The ``<attached-files>`` block must lead the cleaned text
    (so the agent reads the file list BEFORE the prompt).
  * Empty / no-mention input passes through unchanged with an
    empty paths list.
"""

from __future__ import annotations

from ember_code.core.utils.mentions import process_file_mentions


class TestNoMentions:
    def test_empty_string(self):
        # Defensive — composer may strip down to empty before
        # submitting. Don't crash, don't fabricate a hint block.
        cleaned, paths = process_file_mentions("")
        assert cleaned == ""
        assert paths == []

    def test_plain_text(self):
        # No ``@`` anywhere → original text passes through and
        # no hint block is added (a hint block with empty file
        # list would just be noise to the agent).
        cleaned, paths = process_file_mentions("just a normal sentence")
        assert cleaned == "just a normal sentence"
        assert paths == []

    def test_at_without_path(self):
        # Bare ``@`` followed by whitespace is not a mention —
        # the regex requires at least one non-whitespace char
        # after ``@``.
        cleaned, paths = process_file_mentions("trailing @ here")
        assert paths == []


class TestSingleMention:
    def test_basic_mention_extracted(self):
        cleaned, paths = process_file_mentions("read @src/foo.py please")
        assert paths == ["src/foo.py"]

    def test_mention_is_PRESERVED_in_body(self):
        # The top-level docstring claims "The @ tokens are
        # removed from the body entirely" but the code actually
        # preserves them (the rendered user bubble needs to
        # show the user's literal ``@<path>`` reference inline).
        # Pin actual behaviour — anyone trying to "fix" this
        # by stripping breaks the FE bubble rendering.
        cleaned, paths = process_file_mentions("read @src/foo.py please")
        assert "@src/foo.py" in cleaned

    def test_hint_block_prepended(self):
        # The ``<attached-files>`` wrapper carries the file
        # list to the agent. Must come BEFORE the prompt body
        # so the agent reads it first.
        cleaned, _ = process_file_mentions("read @x.py please")
        assert cleaned.startswith("<attached-files>")
        # And the prompt body follows after the closing tag.
        assert "</attached-files>" in cleaned
        assert "read @x.py please" in cleaned

    def test_hint_includes_path(self):
        # The hint body lists each path so the agent knows
        # what to load.
        cleaned, _ = process_file_mentions("see @docs/RFC.md")
        assert "docs/RFC.md" in cleaned

    def test_hint_includes_instruction(self):
        # The "read before responding" suffix is what makes the
        # agent actually open the files. Pin the wording so a
        # refactor doesn't drop it.
        cleaned, _ = process_file_mentions("@x.py")
        assert "read before responding" in cleaned


class TestMentionPosition:
    def test_mention_at_start(self):
        # Start-of-string: the regex's ``(?:^|(?<=\s))`` lookbehind
        # explicitly allows start-of-string.
        cleaned, paths = process_file_mentions("@src/foo.py is broken")
        assert paths == ["src/foo.py"]

    def test_mention_at_end(self):
        # End-of-string: no trailing whitespace, still works.
        # The regex matches greedily to the next whitespace OR
        # end-of-string.
        cleaned, paths = process_file_mentions("see @src/foo.py")
        assert paths == ["src/foo.py"]

    def test_mention_after_newline(self):
        # ``\s`` in the regex includes newlines — mentions
        # right after a line break still match.
        cleaned, paths = process_file_mentions("line one\n@second.py is here")
        assert paths == ["second.py"]


class TestEmailLikeRejection:
    """Email addresses contain ``@`` but must NOT trigger a
    mention. The regex's lookbehind requires whitespace or
    start-of-string BEFORE the ``@`` — load-bearing because
    users absolutely do paste emails in prompts."""

    def test_email_is_not_a_mention(self):
        cleaned, paths = process_file_mentions("email me at user@example.com")
        assert paths == []
        # And no hint block was prepended.
        assert not cleaned.startswith("<attached-files>")

    def test_multiple_emails_in_text(self):
        cleaned, paths = process_file_mentions("alice@a.com and bob@b.com")
        assert paths == []

    def test_email_mixed_with_real_mention(self):
        # Tricky case — email AND a real ``@file`` mention in
        # one message. Only the file should be picked up.
        cleaned, paths = process_file_mentions("ping user@example.com about @src/auth.py")
        assert paths == ["src/auth.py"]


class TestMultipleMentions:
    def test_two_distinct_mentions(self):
        cleaned, paths = process_file_mentions("compare @a.py and @b.py")
        assert paths == ["a.py", "b.py"]

    def test_paths_listed_in_appearance_order(self):
        # Order matters — the agent reads the hint linearly, so
        # the file list mirrors the user's mention order.
        cleaned, paths = process_file_mentions("@third.py after @first.py")
        assert paths == ["third.py", "first.py"]

    def test_duplicate_mention_collected_twice(self):
        # The regex doesn't dedup; each occurrence yields one
        # entry. The agent reads the same file twice — fine,
        # the BE handles it. Pin the behaviour so a future
        # dedup pass surfaces as a deliberate choice.
        cleaned, paths = process_file_mentions("@x.py and again @x.py")
        assert paths == ["x.py", "x.py"]

    def test_hint_joins_paths_with_comma(self):
        # The hint list separator is ``, ``. The agent's
        # parser depends on this format.
        cleaned, _ = process_file_mentions("@a.py @b.py @c.py")
        assert "a.py, b.py, c.py" in cleaned


class TestPathShapes:
    def test_absolute_path_mention(self):
        # Absolute paths starting with ``/``.
        cleaned, paths = process_file_mentions("read @/etc/hosts")
        assert paths == ["/etc/hosts"]

    def test_relative_path_with_dots(self):
        cleaned, paths = process_file_mentions("see @../sibling/foo.py")
        assert paths == ["../sibling/foo.py"]

    def test_path_with_special_chars(self):
        # Non-whitespace chars including hyphens, dots, slashes —
        # all part of the path token. The regex's ``\S+`` is
        # generous on purpose.
        cleaned, paths = process_file_mentions("@my-file.test.ts is here")
        assert paths == ["my-file.test.ts"]
