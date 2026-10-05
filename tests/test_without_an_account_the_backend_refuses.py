"""No account, no documents — and the backend says so rather than shrugging.

Documents live on the organisation's server. A session that cannot reach
one has nothing to show, and the UI walls the whole app behind sign-in
(`App.tsx`). But the wall is a screen, and the backend answers a socket:
anything reaching it another way — a second client, a script, a panel
that renders before `status` arrives — used to get an **empty tree**.

Empty is the wrong answer. It says "you have no documents" when the
truth is "your documents exist, on a server this session cannot reach".
One of those is a prompt to sign in; the other is a reason to go looking
for where everything went.

So the backend walls too, with a sentence that distinguishes the two
cases it can be in:

* the feature is switched off — "Artifacts are disabled."
* nobody is signed in — "Sign in to igni…"

Twenty-one call sites previously handled only the first.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from ember_code.backend.server_artifacts import ArtifactController

_SOURCE = Path(inspect.getfile(ArtifactController)).read_text()


class _Session:
    def __init__(self, *, index: Any) -> None:
        self.artifacts = index
        self.artifact_mgr = None
        self.settings = None


class _SignedOut:
    """A sync client built without credentials, as the real one is."""

    is_configured = False


class _SignedIn:
    is_configured = True


def _controller(index: Any, *, server: Any = None) -> ArtifactController:
    """A controller reaching the server the way every path here does.

    Through `_collaboration()`. The first cut of this read
    `index.shared`, which no code in the controller uses — so the
    guard consulted an accessor nobody else had, and refused sessions
    that were signed in perfectly well. Twenty-four call sites against
    nought should have been the tell.
    """
    controller = ArtifactController.__new__(ArtifactController)
    controller._session = _Session(index=index)  # type: ignore[attr-defined]
    controller._collaboration = lambda: server  # type: ignore[method-assign]
    return controller


class TestTheReasonItGives:
    def test_no_account_asks_for_sign_in(self):
        reason = _controller(object(), server=_SignedOut())._unavailable()

        assert 'Sign in' in reason
        assert 'server' in reason, 'the sentence should say where the documents are'

    def test_disabled_says_disabled(self):
        """A deployment that switched documents off is not a deployment
        somebody can sign into differently."""
        assert _controller(None)._unavailable() == 'Artifacts are disabled.'

    def test_a_signed_in_session_is_not_refused(self):
        assert _controller(object(), server=_SignedIn())._unavailable() == ''

    def test_the_two_reasons_are_different_sentences(self):
        """Told apart because the remedies differ: one is a sign-in,
        the other is a configuration change nobody using the app can
        make."""
        assert _controller(None)._unavailable() != _controller(object(), server=_SignedOut())._unavailable()


class TestTheTreeDoesNotAnswerEmpty:
    async def test_it_refuses_instead_of_returning_no_documents(self):
        """The specific regression. An empty tree reads as an empty
        account — a different and wrong claim."""
        tree = await _controller(object(), server=_SignedOut()).tree()

        assert tree.error
        assert 'Sign in' in tree.error
        assert tree.nodes == []


class TestEveryEntryPointIsBehindIt:
    """Asserted structurally, because the failure is an *omission*.

    A new artifact method that forgets the guard does not break
    anything: it answers from nothing and looks like an empty account.
    Nothing else in the suite would notice.
    """

    def test_nothing_checks_the_index_without_asking_why(self):
        """`if self._session.artifacts is None` was the old shape and
        covers only half the cases. Finding one means a path that
        cannot tell "switched off" from "not signed in".

        `_unavailable` is the exception: deciding that is the first
        half of what it is for.
        """
        lines = _SOURCE.splitlines()
        stragglers = [
            f'line {i + 1}'
            for i, line in enumerate(lines)
            if 'if self._session.artifacts is None:' in line
            and 'return "Artifacts are disabled."' not in (lines[i + 1] if i + 1 < len(lines) else '')
        ]

        assert not stragglers, f'guard(s) still testing the index directly: {stragglers}'

    def test_the_literal_only_follows_a_missing_collaborator(self):
        """"Artifacts are disabled." is the half answer everywhere
        except where it is the *whole* answer: a draft desk or a query
        planner that could not be built, which a sign-in does not fix.

        Asserted as the rule rather than as a count. The first version
        of this allowed "at most six", which was my arithmetic rather
        than the code's — and a magic number here rots into either a
        false alarm or a rubber stamp the moment a method is added.
        """
        lines = _SOURCE.splitlines()
        offenders = []
        for i, line in enumerate(lines):
            if '"Artifacts are disabled."' not in line:
                continue
            # The definition itself, and the docstring explaining it.
            if 'return "Artifacts are disabled."' in line or line.strip().startswith('call sites'):
                continue
            previous = lines[i - 1] if i else ''
            if 'desk is None' in previous or 'planner is None' in previous:
                continue
            offenders.append(f'line {i + 1}: {line.strip()}')

        assert not offenders, 'refusals that cannot say "sign in":\n' + '\n'.join(offenders)

    @pytest.mark.parametrize(
        'method',
        ['tree', 'provenance', 'candidate_evidence', 'export', 'save_draft', 'publish_draft'],
    )
    def test_the_main_entry_points_ask(self, method):
        body = inspect.getsource(getattr(ArtifactController, method))

        assert '_unavailable()' in body, f'{method} does not wall'
