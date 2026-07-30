"""Unit tests for ``session/agent_factory.py``.

The factory helpers were extracted from ``session/core.py`` in
iter 138 (a monster refactor that pulled ~78 LoC out of the
2760-line god-file into this focused module). Session's own
tests exercise the delegation path; these tests pin the pure
functions in isolation so a future refactor of Agno's guardrail
package shape (or the `Settings` sub-model layout) can't
silently regress the "None means feature disabled" contract.
"""

from ember_code.core.config.settings import GuardrailsConfig, ReasoningConfig, Settings
from ember_code.core.session.agent_factory import create_guardrails, create_reasoning_tools


class TestCreateReasoningTools:
    def test_disabled_returns_none(self):
        s = Settings(reasoning=ReasoningConfig(enabled=False))
        assert create_reasoning_tools(s) is None

    def test_enabled_returns_reasoning_tools(self):
        # When enabled, we get an Agno ReasoningTools instance
        # back. The specific class comes from agno; we just check
        # it's not None (session/core.py appends it into the tool
        # list unconditionally, so the None-vs-not distinction is
        # what matters).
        s = Settings(reasoning=ReasoningConfig(enabled=True))
        tools = create_reasoning_tools(s)
        assert tools is not None

    def test_add_instructions_flag_forwarded(self):
        # ``add_instructions`` is a public Agno kwarg — verifying it
        # reaches the constructor guards against a signature drift
        # (Agno renaming the field would silently drop the flag).
        s = Settings(
            reasoning=ReasoningConfig(
                enabled=True,
                add_instructions=True,
                add_few_shot=False,
            )
        )
        tools = create_reasoning_tools(s)
        assert tools is not None
        # Agno stores it as an instance attr with the same name.
        # Only check ``add_instructions`` — ``add_few_shot`` isn't
        # exposed as a public attribute on the current Agno version,
        # so we'd be testing the sentinel default from getattr().
        assert getattr(tools, "add_instructions", False) is True


class TestCreateGuardrails:
    def test_all_disabled_returns_none(self):
        # "Empty list" degrades to None so Session doesn't set
        # ``pre_hooks=[]`` (which Agno would still evaluate as
        # "run these zero hooks" — semantically same as None but
        # noisier in the debug output).
        s = Settings(
            guardrails=GuardrailsConfig(
                pii_detection=False,
                prompt_injection=False,
                moderation=False,
            )
        )
        assert create_guardrails(s) is None

    def test_pii_flag_produces_one_hook(self):
        s = Settings(
            guardrails=GuardrailsConfig(
                pii_detection=True,
                prompt_injection=False,
                moderation=False,
            )
        )
        hooks = create_guardrails(s)
        assert hooks is not None
        assert len(hooks) == 1

    def test_all_three_flags_produce_three_hooks(self):
        s = Settings(
            guardrails=GuardrailsConfig(
                pii_detection=True,
                prompt_injection=True,
                moderation=True,
            )
        )
        hooks = create_guardrails(s)
        assert hooks is not None
        assert len(hooks) == 3
