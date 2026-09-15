"""`docs/APP_TEST_MATRIX.md` must name every function the app exposes.

The matrix is the list of what to test and how far it has got. A list
like that is worth exactly as much as its completeness: a method added
to the protocol and not to the document is a function nobody will think
to drive, and the document will still look finished.

So the methods are read out of ``protocol/rpc.py`` and compared. Adding
an RPC without a row fails here, and the failure names the method.

The rule runs the other way too. A row naming a method that does not
exist makes the document look more thorough than it is, and I wrote one
while drafting it — ``codeindex_cypher``, which is a *tool* the model
can call, not an RPC the client can. It was caught by this check on the
first run.

**Edge cases are deliberately not checked.** They are prose, and a rule
that demanded a non-empty cell would be satisfied by a dash. What a
rule can establish is that the function is *listed*; whether the edge
cases beside it are the right ones is a judgement, and pretending
otherwise is how a document becomes a formality.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_RPC = _ROOT / "src/ember_code/protocol/rpc.py"
_MATRIX = _ROOT / "docs/APP_TEST_MATRIX.md"

#: Statuses a row may carry. Anything else is a typo that would quietly
#: drop the row out of the counts somebody reads.
_STATUSES = {"verified", "answers", "todo", "blocked"}


def _protocol_methods() -> set[str]:
    """Every wire method name in the ``RpcMethod`` enum."""
    return set(re.findall(r'^\s+[A-Z_0-9]+\s*=\s*"([a-z_0-9]+)"', _RPC.read_text(), re.M))


def _matrix_rows() -> list[tuple[str, str]]:
    """``(method, status)`` for every table row in the matrix."""
    rows = []
    for line in _MATRIX.read_text().splitlines():
        match = re.match(r"^\|\s*`([a-z_0-9]+)`\s*\|.*\|\s*`([a-z]+)`\s*\|\s*$", line)
        if match:
            rows.append((match.group(1), match.group(2)))
    return rows


class TestBothSidesWereRead:
    """A parser returning nothing makes every comparison below vacuous."""

    def test_the_protocol_has_methods(self):
        assert len(_protocol_methods()) > 50

    def test_the_matrix_has_rows(self):
        assert len(_matrix_rows()) > 50


class TestTheMatrixMatchesTheProtocol:
    def test_every_method_is_listed(self):
        listed = {m for m, _ in _matrix_rows()}
        missing = sorted(_protocol_methods() - listed)

        assert missing == [], (
            f"{missing} are in the protocol and not in docs/APP_TEST_MATRIX.md. "
            "A function nobody lists is a function nobody drives."
        )

    def test_no_row_names_a_method_that_does_not_exist(self):
        listed = {m for m, _ in _matrix_rows()}
        invented = sorted(listed - _protocol_methods())

        assert invented == [], (
            f"{invented} appear in the matrix and not in the protocol. A row for a "
            "function that does not exist makes the document look more thorough "
            "than it is."
        )

    def test_every_row_carries_a_status_that_means_something(self):
        wrong = sorted({s for _, s in _matrix_rows()} - _STATUSES)

        assert wrong == [], f"unknown statuses {wrong}; expected one of {sorted(_STATUSES)}"

    def test_no_method_is_listed_twice(self):
        listed = [m for m, _ in _matrix_rows()]
        duplicated = sorted({m for m in listed if listed.count(m) > 1})

        assert duplicated == [], (
            f"{duplicated} appear in more than one row, so the totals count them twice."
        )
