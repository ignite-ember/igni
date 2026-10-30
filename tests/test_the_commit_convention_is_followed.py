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

#: The retired company trailer, in both spellings. The rule was written with
#: ``igniteember.sh`` and the history carries ``ignite-ember.sh``; a check that
#: knew only the rule's own typo would pass over every real occurrence.
_RETIRED = re.compile(r"ignite-?ember\.sh", re.I)


#: Where Rule 5 stops naming accepted trailers and starts explaining itself.
#: The retired trailer is quoted below this line, in prose about why it went,
#: and reading it as accepted would make the check vacuous.
_EXPLANATION = "**What this rule said before"


def _rule_five() -> str:
    return _STANDARDS.read_text().split("### Rule 5")[1].split("\n### ")[0]


def _documented_trailer() -> str:
    """The model-attribution trailer — the one an author writes."""
    return _accepted_trailers()[0]


def _accepted_trailers() -> list[str]:
    """Every trailer Rule 5 accepts, read from the document, in order.

    There are two and they arrive by different routes. The first is written by
    whoever makes the commit and names the model that assisted. The second is
    appended by GitHub on every squash merge, naming the author of the branch
    being squashed — nobody types it and it cannot be declined while squash
    merges are in use.

    Only the part *above* the explanation counts. The retired trailer is quoted
    further down, inside the paragraph about why it was retired, and counting it
    as accepted would turn the check below into a check of nothing.
    """
    accepted = _rule_five().split(_EXPLANATION)[0]
    found = [t.strip("`").strip() for t in _TRAILER.findall(accepted)]

    assert found, "Rule 5 no longer names a Co-Authored-By trailer at all"
    return found


def _is_shallow() -> bool:
    """Whether this checkout has real history behind HEAD.

    ``actions/checkout`` fetches depth 1 by default, so in CI the merge commit
    arrives with no parents. Every conclusion this file draws is then wrong in
    the same direction: ``--no-merges`` cannot recognise a merge whose parents
    are absent, so the window is the merge commit itself, and a rule about
    trailers ends up reading "Merge 87490b3 into 708e98a".

    Skipped rather than worked around. The alternative is ``fetch-depth: 0`` on
    the job, which is a CI decision, not something a test should paper over by
    quietly checking nothing.
    """
    result = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "true"


def _rule_landed() -> str:
    """The commit that brought this rule to the branch under test.

    A rule governs from when it exists, not backwards. This file and Rule 5
    lived on a feature branch and arrived on ``main`` in one squash, so the
    window below would otherwise hold forty commits written under whatever
    convention preceded it — all published, none correctable, which is the
    "failure nobody can act on" that :data:`_DEPTH` already exists to avoid.

    Derived from history rather than pinned to a sha so it stays true after the
    next rename or move: whichever commit added this file is the one the rule
    starts at.
    """
    result = subprocess.run(  # noqa: S603
        [
            "git",
            "log",
            "--diff-filter=A",
            "--format=%H",
            "-1",
            "--",
            str(pathlib.Path(__file__).name),
        ],
        cwd=_ROOT / "tests",
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _history() -> list[tuple[str, str]]:
    """``(sha, body)`` for recent non-merge commits, newest first."""
    if _is_shallow():
        pytest.skip(
            "shallow checkout: HEAD has no parents here, so there is no history "
            "to hold to the rule. Set fetch-depth: 0 on this job to enforce it in CI."
        )
    landed = _rule_landed()
    # Excluding the landing commit itself, not just its parents: it was written
    # before the rule existed here, so it is the last commit *without* the rule
    # rather than the first one under it. It does in fact carry a trailer Rule 5
    # does not name, which is how this distinction came up.
    rev = ["HEAD"] + ([f"^{landed}"] if landed else [])
    result = subprocess.run(  # noqa: S603
        ["git", "log", f"-{_DEPTH}", "--no-merges", "--format=%H%x00%B%x01", *rev],
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

    def test_it_also_names_the_trailer_the_forge_adds(self):
        """The second accepted trailer is not a convention anyone follows — it
        is what GitHub writes on a squash merge. Before it was named here, the
        rule disagreed with every merge commit on main, and the only reason
        that was survivable is that the check never ran."""
        accepted = _accepted_trailers()

        assert len(accepted) == 2, accepted
        assert _address(accepted[0]) != _address(accepted[1])

    def test_the_placeholder_address_is_admitted_to_be_one(self):
        """``dev@example.com`` is whatever ``git config user.email`` happens to
        say, not an address that reaches anybody. Naming it without saying so
        would read as a deliberate choice to the next person."""
        section = _rule_five()

        assert "placeholder" in section
        assert "user.email" in section

    def test_the_explanation_marker_is_still_there(self):
        """The accepted trailers are the ones above this marker. If the heading
        is reworded, the retired trailer quoted below it silently becomes
        accepted — so the split point is asserted, not assumed."""
        assert _EXPLANATION in _rule_five()

    def test_the_document_records_why_it_changed(self):
        """Without the reason, the obvious tidy-up is to put the old rule
        back — it reads like somebody simply stopped following it."""
        section = _STANDARDS.read_text().split("### Rule 5")[1].split("\n### ")[0]

        assert "never followed" in section
        assert "attribution was backwards" in section

    def test_there_is_history_to_check(self):
        """A rule over an empty log passes for the wrong reason."""
        history = _history()
        if len(history) <= 5:
            pytest.skip(
                "the rule landed recently and there are few commits under it yet — "
                "nothing here for it to be about."
            )
        assert len(history) > 5, len(history)


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
    def test_every_co_authored_commit_names_a_documented_author(self):
        expected = {_address(t) for t in _accepted_trailers()}
        offenders = []
        for sha, body in _history():
            for found in _TRAILER.findall(body):
                if _address(found) not in expected:
                    offenders.append(f"{sha}: {found.strip()!r}")

        assert not offenders, (
            f"these commits carry a Co-Authored-By that Rule 5 does not name "
            f"(accepted addresses: {sorted(expected)}): {offenders}"
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
        history = _history()
        # Same boundary as the sibling above. "Is the convention in use" needs
        # commits written under it, and right after the rule lands there are
        # none — which would fail for the one reason that is not a finding.
        if len(history) <= 5:
            pytest.skip(
                "the rule landed recently and there are few commits under it yet — "
                "nothing here for it to be about."
            )
        using = [
            sha
            for sha, body in history
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
            f"{sha}: {found.strip()!r}"
            for sha, body in _history()
            for found in _TRAILER.findall(body)
            if _RETIRED.search(found)
        ]

        assert not offenders, offenders

    def test_that_narrowing_still_catches_the_thing_it_is_for(self):
        """A rule made narrower has to be shown to still bite. This is
        the trailer it exists to reject, as it would appear in a real
        commit body."""
        body = "Some change\n\nCo-Authored-By: Ignite Ember <noreply@igniteember.sh>\n"
        found = [t for t in _TRAILER.findall(body) if _RETIRED.search(t)]

        assert found, "the narrowed check no longer recognises the retired trailer"

        # The hyphenated spelling is the one the history actually holds — the
        # rule's own typo is quoted in CODE_STANDARDS.md. Matching only the typo
        # would have been a check that could never fire on a real commit.
        hyphenated = "Some change\n\nCo-Authored-By: Ignite Ember <noreply@ignite-ember.sh>\n"
        assert [t for t in _TRAILER.findall(hyphenated) if _RETIRED.search(t)]

        prose = "It said `Co-Authored-By: Ignite Ember <noreply@igniteember.sh>` and was wrong.\n"
        assert not [t for t in _TRAILER.findall(prose) if _RETIRED.search(t)], (
            "the check still fires on prose that merely mentions the address"
        )
