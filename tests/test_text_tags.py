"""Tests for ``core.text_tags.split_inline_think_tags``.

Pure-function tests; the parser is stateless across calls — the
:class:`ThinkParseState` is passed in and returned out. Each test
exercises one wire-shape quirk.

The load-bearing case is :class:`TestBlockSpansManyEvents`: a
``<think>`` block routinely spans several tag-free content events,
and the parser must keep classifying the middle events as thinking
even though they carry no tags. A string-only carry could not hold
that state — its absence scrambled reasoning across bubbles.
"""

from __future__ import annotations

from ember_code.core.text_tags import (
    INITIAL_STATE,
    ThinkParseState,
    split_inline_think_tags,
)


def _types(segments: list[tuple[str, bool]]) -> list[bool]:
    return [s[1] for s in segments]


def _texts(segments: list[tuple[str, bool]]) -> list[str]:
    return [s[0] for s in segments]


# ── Baseline cases ────────────────────────────────────────────────


class TestBaseline:
    def test_plain_visible_text_emits_single_visible_segment(self):
        segs, state = split_inline_think_tags(INITIAL_STATE, "hello world")
        assert segs == [("hello world", False)]
        assert state == INITIAL_STATE

    def test_empty_text_with_no_state_emits_nothing(self):
        segs, state = split_inline_think_tags(INITIAL_STATE, "")
        assert segs == []
        assert state == INITIAL_STATE

    def test_empty_text_with_fragment_preserves_fragment(self):
        # Model emitted a tiny Agno event (empty content) while we
        # still had a partial tag. The fragment must survive untouched.
        start = ThinkParseState(fragment="<thi")
        segs, state = split_inline_think_tags(start, "")
        assert segs == []
        assert state.fragment == "<thi"

    def test_complete_open_and_close_in_one_event(self):
        segs, state = split_inline_think_tags(INITIAL_STATE, "pre <think>reasoning</think> post")
        assert segs == [
            ("pre ", False),
            ("reasoning", True),
            (" post", False),
        ]
        assert not state.in_thinking
        assert state.fragment == ""


# ── The core bug: a think block spanning many tag-free events ─────


class TestBlockSpansManyEvents:
    """The MiniMax-M2.7 shape (captured from a live BE trace): the
    model opens ``<think>`` in one event, streams reasoning across
    several TAG-FREE events, then closes much later. Every middle
    event must classify as thinking."""

    def test_open_then_tagless_middle_then_close(self):
        events = [
            "<think>\nThe user",
            " is greeting me. No tools needed,",
            " just a direct reply.",
            "\n</think>\n\nHey there!",
        ]
        state = INITIAL_STATE
        seen: list[tuple[str, bool]] = []
        for ev in events:
            segs, state = split_inline_think_tags(state, ev)
            seen.extend(segs)
        # All reasoning is thinking; only the final greeting is visible.
        thinking = "".join(t for t, is_t in seen if is_t)
        visible = "".join(t for t, is_t in seen if not is_t)
        assert "The user is greeting me" in thinking
        assert "just a direct reply" in thinking
        assert "Hey there!" in visible
        assert "The user" not in visible
        assert "greeting" not in visible
        assert not state.in_thinking

    def test_middle_events_stay_thinking(self):
        # After <think> opens, a bare tag-free event is reasoning.
        _segs1, s1 = split_inline_think_tags(INITIAL_STATE, "<think>alpha")
        assert s1.in_thinking is True
        segs2, s2 = split_inline_think_tags(s1, " beta gamma")
        assert segs2 == [(" beta gamma", True)]
        assert s2.in_thinking is True
        segs3, s3 = split_inline_think_tags(s2, " delta</think>visible")
        assert segs3 == [(" delta", True), ("visible", False)]
        assert s3.in_thinking is False


# ── Cross-event tag fragmentation ─────────────────────────────────


class TestCarryOver:
    def test_open_tag_split_across_two_events(self):
        segs1, s1 = split_inline_think_tags(INITIAL_STATE, "<thi")
        assert segs1 == []
        assert s1.fragment == "<thi"
        segs2, s2 = split_inline_think_tags(s1, "nk>reasoning</think>done")
        assert segs2 == [("reasoning", True), ("done", False)]
        assert s2.fragment == ""

    def test_close_tag_split_across_two_events(self):
        segs1, s1 = split_inline_think_tags(INITIAL_STATE, "<think>thinking</t")
        assert segs1 == [("thinking", True)]
        assert s1.fragment == "</t"
        assert s1.in_thinking is True
        segs2, s2 = split_inline_think_tags(s1, "hink>after")
        assert segs2 == [("after", False)]
        assert s2.fragment == ""

    def test_partial_open_remainder_carries_forward(self):
        _segs, s1 = split_inline_think_tags(INITIAL_STATE, "<th")
        assert s1.fragment == "<th"
        segs2, s2 = split_inline_think_tags(s1, "ink>inside</think>after")
        assert segs2 == [("inside", True), ("after", False)]
        assert s2.fragment == ""


# ── Whitespace-split tag re-glue ──────────────────────────────────


class TestWhitespaceGap:
    """Some wire frames split a tag with whitespace between fragments
    (``"</t"`` + ``"\\n\\nhink>"``). The whitespace lived inside the
    split tag and is not content."""

    def test_double_newline_gap(self):
        segs1, s1 = split_inline_think_tags(INITIAL_STATE, "<think>plan</t")
        assert segs1 == [("plan", True)]
        assert s1.fragment == "</t"
        segs2, s2 = split_inline_think_tags(s1, "\n\nhink>after")
        assert segs2 == [("after", False)]
        assert s2.fragment == ""

    def test_single_space_gap(self):
        _segs1, s1 = split_inline_think_tags(INITIAL_STATE, "<think>plan</t")
        segs2, _s2 = split_inline_think_tags(s1, " hink>after")
        assert segs2 == [("after", False)]

    def test_tab_gap(self):
        _segs1, s1 = split_inline_think_tags(INITIAL_STATE, "<think>plan</t")
        segs2, _s2 = split_inline_think_tags(s1, "\think>after")
        assert segs2 == [("after", False)]


# ── Bare-close resume pattern (post-tool) ─────────────────────────


class TestBareClose:
    def test_close_tag_in_later_event_resumes_visible(self):
        # Model streams reasoning without re-opening <think> after a
        # tool, emits "</th" alone, then completes it.
        segs1, s1 = split_inline_think_tags(INITIAL_STATE, "resumed reasoning")
        assert segs1 == [("resumed reasoning", False)]
        segs2, s2 = split_inline_think_tags(s1, "</th")
        # The dangling partial close retroactively marks us mid-close.
        assert s2.fragment == "</th"
        segs3, s3 = split_inline_think_tags(s2, "ink>\n\nHey!")
        assert segs3 == [("\n\nHey!", False)]
        assert not s3.in_thinking


class TestBareStrayClose:
    """A bare ``</think>`` with no opener: the preceding text was the
    reasoning block (post-tool resume where the open was dropped)."""

    def test_bare_close_emits_preceding_text_as_thinking(self):
        segs, state = split_inline_think_tags(INITIAL_STATE, "resumed reasoning</think>answer")
        assert _texts(segs) == ["resumed reasoning", "answer"]
        assert _types(segs) == [True, False]
        assert state.fragment == ""

    def test_bare_close_splits_cleanly(self):
        segs, _state = split_inline_think_tags(INITIAL_STATE, "preamble</think>visible")
        assert _texts(segs) == ["preamble", "visible"]
        assert _types(segs) == [True, False]


# ── Multiple blocks in one event ──────────────────────────────────


class TestMultiSegment:
    def test_two_complete_blocks_yield_five_segments(self):
        text = "before <think>first</think> between <think>second</think> after"
        segs, state = split_inline_think_tags(INITIAL_STATE, text)
        assert state.fragment == ""
        assert _types(segs) == [False, True, False, True, False]
        assert _texts(segs) == [
            "before ",
            "first",
            " between ",
            "second",
            " after",
        ]

    def test_three_blocks_alternate_roles(self):
        text = "x<think>a</think>y<think>b</think>z<think>c</think>w"
        segs, _state = split_inline_think_tags(INITIAL_STATE, text)
        assert _types(segs) == [False, True, False, True, False, True, False]
        assert _texts(segs) == ["x", "a", "y", "b", "z", "c", "w"]


# ── Cancelled run: unclosed trailing block ────────────────────────


class TestTrailingOpen:
    def test_unclosed_trailing_block_emits_thinking_and_stays_open(self):
        segs, state = split_inline_think_tags(
            INITIAL_STATE, "before <think>partial reasoning with no close"
        )
        assert _types(segs) == [False, True]
        assert segs[0][0] == "before "
        assert "partial reasoning" in segs[1][0]
        # State stays open so a following tag-free event is still
        # reasoning (until a close lands or the run resets).
        assert state.in_thinking is True


# ── Realistic long stream from a live trace ───────────────────────


class TestCarrySurvival:
    def test_full_user_bug_repro_chunks(self):
        # Real fragmented shape: "<thi" + "nk>...body..." + "/thi" tag
        # split, close split with a newline gap, then visible answer.
        chunks = [
            "<thi",
            "nk>\nThe user said",
            "ly.\n",
            "</th",
            "ink>\n\nHey friend!",
        ]
        state = INITIAL_STATE
        seen: list[tuple[str, bool]] = []
        for chunk in chunks:
            segs, state = split_inline_think_tags(state, chunk)
            seen.extend(segs)
        thinking = "".join(t for t, is_t in seen if is_t)
        visible = "".join(t for t, is_t in seen if not is_t)
        assert "The user said" in thinking
        assert "Hey friend!" in visible
        assert "Hey friend!" not in thinking
        assert state.fragment == ""
        assert not state.in_thinking


# ── Robustness / no-op inputs ──────────────────────────────────────


class TestNoOp:
    def test_only_whitespace_emits_nothing(self):
        segs, _state = split_inline_think_tags(INITIAL_STATE, "   \n\n  ")
        assert segs == []

    def test_text_with_just_words_emits_visible(self):
        segs, _state = split_inline_think_tags(INITIAL_STATE, "no tags here")
        assert segs == [("no tags here", False)]
