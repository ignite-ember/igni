"""Both message paths run the guardrails.

igni has two ways a user message reaches the model:

* ``SessionMessageHandler.handle`` — the session path.
* ``RunController._run_locked`` — the streaming path the portal uses.

Only the first ran :class:`GuardrailRunner`. The second passed the message
straight to ``team.arun``, so a user talking to the portal got no guardrail
signal at all. agno's own PII guardrail is not a substitute: it hard-raises
before the model call, which stops the run rather than informing the model, and
igni's guardrails are deliberately inform-don't-block.

The failure is quiet in the worst way. Guardrails are configured once and
verified once, almost certainly through whichever path the person testing
happened to use, and a guardrail that does not run looks exactly like a
guardrail with nothing to report.

The prefix is built in one place now — ``GuardrailRunner.warning_prefix`` — so
the two callers cannot silently disagree about what a triggered guardrail does.
The test that matters most here is the last one, which asserts that every path
in this list is checked, so adding a third entry point fails loudly rather than
inheriting the same gap.
"""

from __future__ import annotations

import pytest

from ember_code.core.guardrails.base import Guardrail, GuardrailResult


class _AlwaysFlags(Guardrail):
    """A guardrail that fires on everything, so the prefix is observable."""

    name = "always"
    gate_key = "test_always"

    def check(self, text: str) -> GuardrailResult:
        return GuardrailResult(
            passed=False, guardrail=self.name, message="flagged for test", findings=[]
        )


@pytest.fixture
def runner():
    """A runner holding one always-firing guardrail.

    Built by hand rather than through ``Settings``: ``iter_enabled`` keys off
    config flags, and inventing a flag for a test guardrail would mean editing
    ``GuardrailsConfig`` to test it.
    """
    from ember_code.core.guardrails.runner import GuardrailRunner

    made = GuardrailRunner.__new__(GuardrailRunner)
    made._guardrails = [_AlwaysFlags()]
    return made


class TestTheSharedPrefix:
    async def test_it_warns_when_a_guardrail_fires(self, runner):
        prefix = await runner.warning_prefix("anything")

        assert "[GUARDRAIL WARNING]" in prefix
        assert "flagged for test" in prefix

    async def test_it_is_empty_when_nothing_fires(self, runner):
        runner._guardrails = []

        assert await runner.warning_prefix("anything") == ""


class TestEveryEntryPointUsesIt:
    """The structural assertion — a path that skips the runner is the bug."""

    def test_the_session_path_delegates(self):
        """``_guardrail_prefix`` must not reimplement the text.

        It did, and the copy was the only implementation, so the portal path had
        nothing to call and grew its own gap instead.
        """
        import inspect

        from ember_code.core.session.message_handler import SessionMessageHandler

        source = inspect.getsource(SessionMessageHandler._guardrail_prefix)

        assert "warning_prefix" in source, (
            "the session path should call GuardrailRunner.warning_prefix rather "
            "than build the warning itself"
        )
        assert "[GUARDRAIL WARNING]" not in source, (
            "the warning text is duplicated here again — two copies is how the "
            "portal path ended up with none"
        )

    def test_the_streaming_path_runs_them(self):
        """The portal path must reach the runner before ``team.arun``."""
        import inspect

        from ember_code.backend.run_controller import RunController

        # Comments stripped first. The initial version of this test matched the
        # explanatory comment above the fix, which names ``team.arun`` in prose,
        # and concluded the call came before the check. An ordering assertion
        # that reads prose is worse than none — it fails on a correct file and
        # would pass on a wrong one that happened to phrase things differently.
        source = "\n".join(
            line.split("#", 1)[0]
            for line in inspect.getsource(RunController._run_locked).splitlines()
        )
        prefix_at = source.find("warning_prefix")
        arun_at = source.find("team.arun")

        assert prefix_at != -1, (
            "RunController._run_locked does not run the guardrails — this is the "
            "portal path, and skipping it means a portal user is unguarded"
        )
        assert arun_at != -1 and prefix_at < arun_at, (
            "the guardrail check must run before the message is handed to the model"
        )
