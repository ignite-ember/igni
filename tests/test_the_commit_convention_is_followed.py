"""Rule 5 of `CODE_STANDARDS.md`, checked against the history.

The rule said commits use `Co-Authored-By: Ignite Ember
<noreply@igniteember.sh>` — "Not Claude." **Not one commit in the
history has ever done that.** Every commit carrying a co-author names
the model.

So the standard was decorative, and that is worse than not having it: a
reader cannot tell which rules in that file are real, so they discount
all of them. The rule now states what the repository actually does, and
this test is what stops the two drifting apart again — the expected
trailer is *read out of the document*, not repeated here, so there is
one place to change and no way to change one without the other.

The rule is conditional on purpose. A commit written by a person alone
carries no co-author, and demanding one would either be false or push
people to add a trailer that is not true. What is checked is: **if a
commit claims a co-author, it is the documented one.**
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_STANDARDS = _ROOT / "CODE_STANDARDS.md"

#: How many commits back to look. Enough to cover recent practice
#: without turning a years-old convention change into a failure that
#: nobody can act on — the rule is about what we do now.
_DEPTH = 40

#: Matches the trailer both in a commit body (bare, at line start) and
#: in the document (wrapped in backticks). Without the optional
#: backtick the parse found nothing in `CODE_STANDARDS.md` and every
#: check below failed with "Rule 5 no longer names a trailer" — a
#: parser that cannot read its own source of truth.
_TRAILER = re.compile(r"^`?Co-Authored-By:\s*(.+?)`?\s*$", re.M | re.I)


def _documented_trailer() -> str:
    """The one trailer Rule 5 names, read from the document."""
    section = _STANDARDS.read_text().split("### Rule 5")[1].split("\n### ")[0]
    found = _TRAILER.findall(section)

    assert found, "Rule 5 no longer names a Co-Authored-By trailer at all"
    # The section quotes the accepted trailer first and the retired one
    # inside the explanation, so the first match is the live one.
    return found[0].strip("`").strip()


def _history() -> list[tuple[str, str]]:
    """``(sha, body)`` for recent non-merge commits, newest first."""
    result = subprocess.run(  # noqa: S603
        ["git", "log", f"-{_DEPTH}", "--no-merges", "--format=%H%x00%B%x01"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"git history is not available here: {result.stderr.strip()}")

    out = []
    for entry in result.stdout.split("\x01"):
        entry = entry.strip()
        if not entry:
            continue
        sha, _, body = entry.partition("\x00")
        out.append((sha[:8], body))
    return out


class TestTheRuleIsReadable:
    """If the document stops naming a trailer, every check below turns
    vacuous rather than failing — so the parse is asserted first."""

    def test_rule_five_exists_and_names_a_trailer(self):
        trailer = _documented_trailer()

        assert "@" in trailer, trailer
        assert trailer.startswith("Claude"), (
            f"Rule 5 names {trailer!r}. If the convention has genuinely changed, this "
            "assertion is the place to say so deliberately."
        )

    def test_the_document_records_why_it_changed(self):
        """Without the reason, the obvious tidy-up is to put the old rule
        back — it reads like somebody simply stopped following it."""
        section = _STANDARDS.read_text().split("### Rule 5")[1].split("\n### ")[0]

        assert "never followed" in section
        assert "attribution was backwards" in section

    def test_there_is_history_to_check(self):
        """A rule over an empty log passes for the wrong reason."""
        assert len(_history()) > 5, len(_history())


def _address(trailer: str) -> str:
    """The `<...>` part, lowercased.

    The comparison is on the address, not the whole string. History
    holds `Claude Opus 5 <noreply@anthropic.com>` alongside
    `Claude Opus 5 (1M context) <noreply@anthropic.com>` — the same
    model, labelled differently as the tooling changed. Demanding a
    byte-exact display name would fail a correct commit for a variant
    nobody can go back and edit, which is a rule strict enough to be
    ignored. The address is what identifies the co-author; the
    parenthetical is a label.
    """
    match = re.search(r"<([^>]+)>", trailer)
    return match.group(1).strip().lower() if match else trailer.strip().lower()


class TestTheHistoryFollowsIt:
    def test_every_co_authored_commit_names_the_documented_author(self):
        expected = _address(_documented_trailer())
        offenders = []
        for sha, body in _history():
            for found in _TRAILER.findall(body):
                if _address(found) != expected:
                    offenders.append(f"{sha}: {found.strip()!r}")

        assert not offenders, (
            f"these commits carry a Co-Authored-By that Rule 5 does not name "
            f"(expected the address {expected!r}): {offenders}"
        )

    def test_the_comparison_is_on_the_address(self):
        """Pinned, because "compare the whole string" is the obvious
        simplification and it breaks on the history as it stands."""
        assert _address("Claude Opus 5 <noreply@anthropic.com>") == _address(
            "Claude Opus 5 (1M context) <noreply@anthropic.com>"
        )
        assert _address("Ignite Ember <noreply@igniteember.sh>") != _address(
            "Claude Opus 5 <noreply@anthropic.com>"
        )

    def test_the_convention_is_actually_in_use(self):
        """The other direction, and the one that catches a rule quietly
        falling out of practice: if nothing in recent history carries the
        trailer, Rule 5 has become decorative again — which is exactly
        the state it was in before.
        """
        expected = _address(_documented_trailer())
        using = [
            sha
            for sha, body in _history()
            if any(_address(t) == expected for t in _TRAILER.findall(body))
        ]

        assert using, (
            f"no commit in the last {_DEPTH} carries {expected!r}. Either the convention "
            "has changed and Rule 5 should say so, or it has quietly stopped being "
            "followed — which is how the previous version of this rule died."
        )

    def test_the_retired_trailer_is_gone_from_practice(self):
        """It was never in practice to begin with — that is the point —
        so this pins that it does not creep back in from somebody reading
        an old copy of the standards.

        Checked against the **trailers**, not the message body. Written
        against the body it fired on the commit that retired the rule,
        because that message quotes the old address in order to explain
        why it went — and a commit message cannot be reworded
        afterwards. A rule that flags the prose describing a change is
        the shape this suite keeps finding; here the fix is to ask the
        narrower question, which is also the one that was meant.
        """
        offenders = [
            f'{sha}: {found.strip()!r}'
            for sha, body in _history()
            for found in _TRAILER.findall(body)
            if 'igniteember.sh' in found
        ]

        assert not offenders, offenders

    def test_that_narrowing_still_catches_the_thing_it_is_for(self):
        """A rule made narrower has to be shown to still bite. This is
        the trailer it exists to reject, as it would appear in a real
        commit body."""
        body = (
            'Some change\n\nCo-Authored-By: Ignite Ember <noreply@igniteember.sh>\n'
        )
        found = [t for t in _TRAILER.findall(body) if 'igniteember.sh' in t]

        assert found, 'the narrowed check no longer recognises the retired trailer'

        prose = 'It said `Co-Authored-By: Ignite Ember <noreply@igniteember.sh>` and was wrong.\n'
        assert not [t for t in _TRAILER.findall(prose) if 'igniteember.sh' in t], (
            'the check still fires on prose that merely mentions the address'
        )
