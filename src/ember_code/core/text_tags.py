"""Pure parser for inline ``<think>`` tags carried inside content streams.

Some LLM providers (notably MiniMax-style "anthropic-clone" wrappers)
emit reasoning inline as literal ``<think>…</think>`` inside the visible
``content`` field, instead of routing it through Agno's dedicated
``reasoning_content`` field. With Agno's reasoning stream empty for
those providers the reasoning would otherwise land in the visible
bubble — looking like the model leaked its scratchpad to the user.

This module is the live-stream counterpart to
:class:`ember_code.backend.restore_content.AssistantContentRestorer` —
that class parses persisted assistant content on history restore
(``AssistantContentRestorer.split_content``). The two paths must
produce identical segment layouts so a turn looks the same live and
on reload.

Why the parser lives on the BE, not the FE
-------------------------------------------

The FE previously carried an equivalent parser — ``splitThinkTags``
in ``clients/web/src/chat/model.ts``. It was fragile because the
wire can split a tag across two ``content_delta`` frames (carry
state required), whitespace can land mid-tag (``"</t"`` +
``"\\n\\nhink>"``), and React 18 ``<StrictMode>`` double-fires the
surrounding reducer, contaminating the 2nd call when carry state is
mutated in place. Moving the parser to the BE eliminates all
three: Agno accumulates deltas into one event payload, the state
lives on a long-lived session object, and there is no React
lifecycle to fight.

Why a state object, not a bare string carry
--------------------------------------------

A ``<think>`` block routinely spans MANY content events: the model
emits ``<think>`` in one event, then streams reasoning across
several tag-free events, then emits ``</think>`` much later. To
classify the tag-free middle events correctly the parser must
remember *"we are currently inside an open think block"* — a single
boolean that a string-only carry cannot hold. :class:`ThinkParseState`
carries both that boolean (``in_thinking``) and any trailing
partial-tag ``fragment`` across events of the same run.

Public surface
--------------

``split_inline_think_tags(state, text)``
    Pure function. Returns ``(segments, next_state)``. ``segments``
    is a list of ``(text, is_thinking)`` tuples; ``next_state`` is a
    :class:`ThinkParseState` the next event of the same run must pass
    back in. Reset to a fresh :class:`ThinkParseState` on ``run_end``
    so a stray partial from a cancelled run never bleeds into the
    next run.
"""

from __future__ import annotations

from dataclasses import dataclass

# Tag constants — built via chr/concat so the literal angle brackets
# aren't stripped by tooling that interprets HTML tags.
_LT = "<"
_GT = ">"

# Multiple tag variants observed in the wild. The original
# Anthropic-style forms are ``<think>`` and ``</think>`` (MiniMax-M2.7
# emits these inline); newer / non-anthropic models sometimes use
# longer tags like ``<thinking>``/``</thinking>``. We support both so
# the parser picks up whichever the model happens to emit. A single
# run rarely mixes styles.
_OPEN_TAGS = tuple(_LT + o + _GT for o in ("thinking", "think"))
_CLOSE_TAGS = tuple(_LT + "/" + o + _GT for o in ("thinking", "think"))
_ALL_TAGS = _OPEN_TAGS + _CLOSE_TAGS
_MAX_TAG_LEN = max(len(t) for t in _ALL_TAGS)


@dataclass(frozen=True)
class ThinkParseState:
    """Cross-event state for :func:`split_inline_think_tags`.

    ``in_thinking``
        ``True`` when a ``<think>`` block opened in a previous event
        has not yet been closed — so every tag-free event that
        follows is reasoning, not visible content. This is the bit a
        string-only carry could not represent, and its absence was
        the "reasoning scrambled across bubbles" bug.
    ``fragment``
        Trailing partial tag from the previous event (e.g. ``"</t"``)
        that must be prepended to the next event's payload before
        scanning.
    """

    in_thinking: bool = False
    fragment: str = ""


#: Fresh state for the first event of a run. Reset to this on
#: ``run_completed`` / ``run_error`` so a cancelled run's partial
#: tag or open block can't leak into the next run.
INITIAL_STATE = ThinkParseState()


def _find_earliest(working: str, tags: tuple[str, ...], start: int) -> tuple[int, str]:
    """Earliest complete occurrence of any tag in ``tags`` at or after
    ``start``. Returns ``(index, matched_tag)`` or ``(-1, "")``."""
    best_idx = -1
    best_tag = ""
    for tag in tags:
        idx = working.find(tag, start)
        if idx != -1 and (best_idx == -1 or idx < best_idx):
            best_idx = idx
            best_tag = tag
    return best_idx, best_tag


def _trailing_tag_fragment(s: str) -> str:
    """Longest suffix of ``s`` that is a *proper* prefix of some tag.

    Every tag begins with ``<``, so only ``<``-led suffixes can carry.
    A complete tag at the very end is consumed by the scan loop, not
    here — we return proper prefixes only (``t != suffix``).
    """
    upper = min(len(s), _MAX_TAG_LEN - 1)
    for n in range(upper, 0, -1):
        suffix = s[-n:]
        if any(t.startswith(suffix) and t != suffix for t in _ALL_TAGS):
            return suffix
    return ""


def _starts_with_full_tag(s: str) -> bool:
    return any(s.startswith(t) for t in _ALL_TAGS)


def split_inline_think_tags(
    state: ThinkParseState,
    text: str,
) -> tuple[list[tuple[str, bool]], ThinkParseState]:
    """Split one Agno content event into ``(text, is_thinking)``
    segments, threading tag state across events of the same run.

    Parameters
    ----------
    state:
        :class:`ThinkParseState` returned by the previous event of
        this run (``INITIAL_STATE`` for the first event).
    text:
        The full payload of one Agno ``RunContentEvent``.

    Returns
    -------
    ``(segments, next_state)``. ``segments`` is a list of
    ``(text, is_thinking)`` tuples ready for the wire; ``next_state``
    carries the open-block flag and any trailing partial tag to the
    next event of the same run.

    Notes
    -----
    Pure: no side effects, safe to call from any thread / coroutine.
    Adjacent same-role segments are collapsed, so the consumer sees
    one ``content_delta`` per role switch, not per token.
    """
    segments: list[tuple[str, bool]] = []
    fragment = state.fragment
    in_thinking = state.in_thinking

    if not text and not fragment:
        return segments, state

    working = fragment + text

    # Whitespace-split tag re-glue: some wire frames split a tag with
    # whitespace between the fragments (``"</t"`` + ``"\n\nhink>"``).
    # If the fragment is a tag prefix and the incoming text opens with
    # whitespace, try gluing on the lstrip'd text — if that completes a
    # real tag the whitespace lived *inside* the split tag and isn't
    # content. (Whitespace *after* a completed tag is preserved.)
    if fragment and text[:1].isspace() and _trailing_tag_fragment(fragment) == fragment:
        candidate = fragment + text.lstrip()
        if _starts_with_full_tag(candidate) and not _starts_with_full_tag(working):
            working = candidate

    cursor = 0
    n = len(working)
    while cursor < n:
        if in_thinking:
            # Inside a think block: only a close tag ends it. Opens are
            # literal text (a nested ``<think>`` is pathological — we
            # don't try to balance it).
            idx, tag = _find_earliest(working, _CLOSE_TAGS, cursor)
            if idx == -1:
                break
            _emit(segments, working[cursor:idx], is_thinking=True)
            cursor = idx + len(tag)
            in_thinking = False
            continue

        # Visible mode: the next boundary is either an open tag (start
        # of reasoning) or a *bare* close tag. A bare close with no
        # matching open is the post-tool resume pattern — the model
        # streamed reasoning without re-opening ``<think>``; the text
        # leading into the close is therefore reasoning.
        idx_o, tag_o = _find_earliest(working, _OPEN_TAGS, cursor)
        idx_c, tag_c = _find_earliest(working, _CLOSE_TAGS, cursor)
        if idx_o == -1 and idx_c == -1:
            break
        if idx_c != -1 and (idx_o == -1 or idx_c < idx_o):
            # Bare close first: preceding text was reasoning.
            _emit(segments, working[cursor:idx_c], is_thinking=True)
            cursor = idx_c + len(tag_c)
            # Stays visible after the close.
        else:
            _emit(segments, working[cursor:idx_o], is_thinking=False)
            cursor = idx_o + len(tag_o)
            in_thinking = True

    # Trailing text after the last complete tag. Peel any partial tag
    # to carry into the next event.
    rest = working[cursor:]
    next_fragment = _trailing_tag_fragment(rest)
    body = rest[: len(rest) - len(next_fragment)] if next_fragment else rest

    body_thinking = in_thinking
    next_in_thinking = in_thinking
    if next_fragment.startswith(_LT + "/"):
        # A partial *close* tag is dangling — we were reasoning right
        # up to it (even from visible mode: the close implies the
        # preceding text was a think block). Stay mid-close so the next
        # event's completion of the close flips us back to visible.
        body_thinking = True
        next_in_thinking = True

    # Skip a whitespace-only tail — nothing to render, and it would
    # otherwise leak a stray space into a bubble.
    if body.strip():
        _emit(segments, body, is_thinking=body_thinking)

    return segments, ThinkParseState(in_thinking=next_in_thinking, fragment=next_fragment)


def _emit(segments: list[tuple[str, bool]], text: str, *, is_thinking: bool) -> None:
    """Push a wire-ready segment, collapsing adjacent same-role
    segments so the consumer sees one ``content_delta`` per role
    switch rather than per token.
    """
    if not text:
        return
    if segments and segments[-1][1] == is_thinking:
        prev_text, prev_role = segments[-1]
        segments[-1] = (prev_text + text, prev_role)
        return
    segments.append((text, is_thinking))


__all__ = ["split_inline_think_tags", "ThinkParseState", "INITIAL_STATE"]
