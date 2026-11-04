"""The learning-context block must replace itself, not stack up.

``SessionLearningManager.inject`` runs on every portal turn. It pruned
the previous block by matching ``"## What I Know About You"`` /
``"## User Profile"`` — headings Agno's learning stores never emit (they
open with ``<user_memory>``, ``<session_context>``, …). Nothing ever
matched, so a 500-2000 token block was appended on each turn and the
model was handed N mutually-redundant copies of its own memory.

The fix makes the manager wrap the body in a heading it owns, so the
prune matches text this codebase wrote rather than text Agno formatted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ember_code.core.session.learning_ops import SessionLearningManager

#: Shaped like the real thing: ``LearningMachine.abuild_context`` joins
#: its stores' contexts, and ``UserMemoryStore`` opens with this tag.
#: See ``agno/learn/stores/user_memory.py``.
_AGNO_CONTEXT = "<user_memory>\n- prefers tabs\n</user_memory>"


class _FakeLearning:
    def __init__(self, ctx: str) -> None:
        self.model = object()
        self.db = object()
        self._ctx = ctx

    async def abuild_context(self, user_id: str, session_id: str) -> str:
        return self._ctx


def _manager(team, ctx: str = _AGNO_CONTEXT) -> SessionLearningManager:
    return SessionLearningManager(
        settings=SimpleNamespace(),
        db=object(),
        user_id_ref=lambda: "u",
        session_id_ref=lambda: "s",
        main_team_ref=lambda: team,
        learning=_FakeLearning(ctx),
    )


@pytest.mark.asyncio
async def test_repeated_injection_does_not_grow_the_prompt():
    team = SimpleNamespace(instructions=["base instructions"])
    mgr = _manager(team)

    for _ in range(5):
        await mgr.inject()

    assert len(team.instructions) == 2, (
        f"the learning block stacked up — every turn added another copy: {team.instructions}"
    )
    assert team.instructions[0] == "base instructions"
    assert team.instructions[1].count("<user_memory>") == 1


@pytest.mark.asyncio
async def test_fresh_context_replaces_the_stale_one():
    team = SimpleNamespace(instructions=["base instructions"])
    mgr_old = _manager(team, "<user_memory>\n- prefers tabs\n</user_memory>")
    await mgr_old.inject()
    mgr_new = _manager(team, "<user_memory>\n- prefers spaces now\n</user_memory>")
    await mgr_new.inject()

    joined = "\n".join(team.instructions)
    assert "prefers spaces now" in joined
    assert "prefers tabs" not in joined, "the superseded memory was left in the prompt"


@pytest.mark.asyncio
async def test_non_string_instructions_are_left_alone():
    # Agno allows callables / non-str entries in ``instructions``; the
    # prune must not blow up on them (the old filter called
    # ``.startswith`` on whatever it found).
    marker = object()
    team = SimpleNamespace(instructions=["base", marker])
    mgr = _manager(team)

    await mgr.inject()

    assert marker in team.instructions
