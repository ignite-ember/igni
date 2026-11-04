"""``CommandAction`` is declared twice, so it is checked.

A command that opens a panel does it by returning an action, which
the backend stamps and ``App.tsx`` switches on. The list exists in
two places — :class:`CommandAction` here and a string union in
``clients/web/src/protocol/messages.ts`` — because the client is
TypeScript and does not import Python.

Two declarations drift. A member added here and not there is a
command that runs, returns, and does nothing: the switch has no
arm, the backend reports success, and the panel never opens. There
is no error anywhere, which is why this is a test and not a code
review item — adding `/extensions` needed both edits and only the
second one failed loudly, and only because its arm happened to use
a literal.

The reverse — a union member with no Python behind it — is a dead
branch rather than a bug, and is reported too: it is usually the
leftover of something removed.
"""

from __future__ import annotations

import re
from pathlib import Path

from ember_code.protocol.schemas.enums import CommandAction

_ROOT = Path(__file__).resolve().parent.parent
_MESSAGES = _ROOT / "clients/web/src/protocol/messages.ts"


def _declared_in_typescript() -> set[str]:
    """The members of the ``CommandAction`` union, as written."""
    text = _MESSAGES.read_text()
    start = text.index("export type CommandAction =")
    end = text.index(";", start)
    return set(re.findall(r'"([^"]*)"', text[start:end]))


class TestBothSidesWereRead:
    """A parser that finds nothing makes every check below vacuous."""

    def test_the_enum_has_members(self):
        assert len(list(CommandAction)) >= 20

    def test_the_union_was_parsed(self):
        assert len(_declared_in_typescript()) >= 20


def test_every_action_has_a_client_that_knows_it():
    """Otherwise the command runs, succeeds, and nothing happens."""
    declared = _declared_in_typescript()
    missing = sorted(str(a) for a in CommandAction if str(a) not in declared)
    assert not missing, (
        f"not in the CommandAction union in {_MESSAGES.name}: {missing}. The "
        "backend will stamp these and the client's switch has no arm, so the "
        "command succeeds and the panel never opens."
    )


def test_the_client_knows_nothing_extra():
    """A union member with no Python behind it is a dead branch.

    ``"none"`` is the exception: the enum spells "no action" as the
    empty string, and the union carries both because an older
    backend sent the word.
    """
    known = {str(a) for a in CommandAction} | {"none"}
    invented = sorted(a for a in _declared_in_typescript() if a not in known)
    assert not invented, (
        f"in the union and not in CommandAction: {invented}. Nothing can send "
        "these, so the arm behind each one is unreachable."
    )
