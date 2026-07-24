"""Regression tests for ``HITLStreamMultiplexer._split_inline_think_tags``.

The parser itself (``core.text_tags``) is unit-tested separately. These
tests pin the *multiplexer* wiring — specifically the "pass the original
message through unchanged" fast paths, which must NOT fire when the
parser stripped a split-tag remainder from the text.

The bug these guard against: a ``</think>`` that splits across two
content events (``…astronomy.\n</thi`` then ``nk>\n\nStars…``) had its
``nk>`` remainder leak into the visible bubble, because the fast path
compared only the segment *role* to the original, not its *text*, and
re-emitted the original (untrimmed) message.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ember_code.backend.hitl_stream_mux import HITLStreamMultiplexer
from ember_code.protocol.schemas.be_events import ContentDelta


@dataclass
class _FakeEvent:
    """Minimal Agno-event stand-in — the method only reads ``type`` and
    ``run_id`` off it via ``getattr``."""

    type: str = "run_content"
    run_id: str = "run-1"


def _mux() -> HITLStreamMultiplexer:
    # ``_split_inline_think_tags`` touches none of the collaborators;
    # it only uses ``self._inline_think_state``. Construct with ``None``
    # stand-ins.
    return HITLStreamMultiplexer(session=None, store=None, pause_handler=None, tracer=None)


async def _drive(mux: HITLStreamMultiplexer, run_id: str, text: str, is_thinking: bool):
    msg = ContentDelta(text=text, is_thinking=is_thinking, id=run_id)
    out = []
    async for m in mux._split_inline_think_tags(_FakeEvent(run_id=run_id), msg):
        out.append(m)
    return out


@pytest.mark.asyncio
async def test_split_close_tag_does_not_leak_remainder():
    """``</think>`` split as ``</thi`` + ``nk>`` must not leak ``nk>``
    into the visible bubble."""
    mux = _mux()
    run = "run-stars"

    # Open + reasoning in one event.
    await _drive(mux, run, "<think>\nStars have colors because of temperature.", False)
    # Reasoning continues, then the close tag begins but splits.
    e2 = await _drive(mux, run, " Blackbody physics.\n</thi", False)
    # The rest of the close tag + the visible answer.
    e3 = await _drive(mux, run, "nk>\n\nStars appear different colors...", False)

    all_out = e2 + e3
    for m in all_out:
        assert "nk>" not in m.text, f"leaked tag remainder: {m.text!r}"
        assert "</thi" not in m.text
        assert "think>" not in m.text

    # The visible answer must be present and tagged non-thinking.
    visible = "".join(m.text for m in all_out if not m.is_thinking)
    assert "Stars appear different colors" in visible
    # The reasoning must be tagged thinking and not contain the answer.
    thinking = "".join(m.text for m in all_out if m.is_thinking)
    assert "Blackbody physics" in thinking
    assert "Stars appear different colors" not in thinking


@pytest.mark.asyncio
async def test_unchanged_visible_text_passes_through_identity():
    """When nothing is stripped, the fast path still returns the exact
    same message instance (identity memoisation on the FE)."""
    mux = _mux()
    msg = ContentDelta(text="just a plain answer", is_thinking=False, id="run-x")
    out = []
    async for m in mux._split_inline_think_tags(_FakeEvent(run_id="run-x"), msg):
        out.append(m)
    assert len(out) == 1
    assert out[0] is msg


@pytest.mark.asyncio
async def test_reasoning_delta_passes_through_untouched():
    """Native reasoning (is_thinking=True) bypasses the parser entirely."""
    mux = _mux()
    msg = ContentDelta(text="some reasoning", is_thinking=True, id="run-y")
    out = []
    async for m in mux._split_inline_think_tags(_FakeEvent(run_id="run-y"), msg):
        out.append(m)
    assert len(out) == 1
    assert out[0] is msg


@pytest.mark.asyncio
async def test_inline_block_in_single_event_splits_to_two_bubbles():
    """A complete ``<think>…</think>`` block followed by the answer in
    one event yields a thinking segment and a clean visible segment."""
    mux = _mux()
    out = await _drive(
        mux,
        "run-z",
        "<think>reasoning here</think>\n\nThe answer is 42.",
        False,
    )
    thinking = "".join(m.text for m in out if m.is_thinking)
    visible = "".join(m.text for m in out if not m.is_thinking)
    assert "reasoning here" in thinking
    assert "The answer is 42" in visible
    for m in out:
        assert "think>" not in m.text
