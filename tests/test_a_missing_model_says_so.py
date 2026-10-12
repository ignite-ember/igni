"""When there is no model, the screen says so.

A source verdict reported **"The model did not answer: Unknown model
error"** on a deployment whose actual problem was that nobody had run
`/login`. Three things lined up to produce that:

* with nothing configured, `ModelRegistry.get_model` hands back a
  `NoModelConfigured` placeholder;
* every invocation of it raises a `ValueError` carrying the right
  sentence — "No model configured. Run `/login` …";
* and Agno does not propagate exceptions out of a run. It catches,
  marks the run ERROR, and puts **its own** words in `content`.

So the one message worth showing was replaced by a generic one on its
way to the screen, and the remedy — which the placeholder knows and
states — was lost.

Asked before the call instead. The placeholder is recognisable by its
type, and asking first also saves a round trip to a host literally
named `placeholder.invalid`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ember_code.backend.server_artifacts import ArtifactController
from ember_code.core.config.null_model import NoModelConfigured


def _controller(model) -> ArtifactController:
    controller = ArtifactController.__new__(ArtifactController)
    controller._session = SimpleNamespace(settings=object(), artifacts=object())
    controller._model_for_planning = lambda: model  # type: ignore[method-assign]
    return controller


class TestKnowingThereIsNoModel:
    def test_the_placeholder_is_recognised(self):
        assert _controller(NoModelConfigured.for_login_required())._no_model()

    def test_and_its_own_sentence_is_what_is_reported(self):
        # The placeholder states the remedy; nothing else needs to
        # invent one.
        reason = _controller(NoModelConfigured.for_login_required())._no_model()

        assert "No model configured" in reason
        assert "/login" in reason

    def test_a_stale_default_says_that_instead(self):
        # A different situation with a different remedy: the model is
        # named in config and the deployment no longer serves it.
        reason = _controller(NoModelConfigured.for_stale_default("gpt-4o"))._no_model()

        assert "gpt-4o" in reason
        assert "not in" in reason

    def test_a_real_model_is_not_reported_as_missing(self):
        # The guard must not stand in front of a working deployment.
        assert _controller(SimpleNamespace(id="MiniMax-M2.7"))._no_model() == ""


class TestWhatEachCallerAnswers:
    """Every path that asks a model has somewhere to put the reason."""

    def test_every_agent_built_here_asks_first(self):
        """Structural, so the next one added cannot forget.

        Faking a whole session per caller would test the fakes. What
        matters is the property: no path constructs an ``Agent``
        without having consulted ``_no_model`` on the way, because the
        one that does is the one that reports "Unknown model error"
        again.
        """
        import inspect

        from ember_code.backend import server_artifacts

        source = inspect.getsource(server_artifacts).split("\n")
        built = [i for i, line in enumerate(source) if "agent = Agent(" in line]
        assert built, "no agents are built here any more — has this moved?"

        for line in built:
            # The guard sits within a handful of lines above the
            # construction, inside the same method.
            window = "\n".join(source[max(0, line - 14) : line])
            assert "_no_model()" in window, (
                f"line {line + 1} builds an Agent without asking whether a model is "
                "configured. Without it, a deployment with none gets "
                '"Unknown model error" — Agno replaces the placeholder\'s own '
                "sentence with its own."
            )

    @pytest.mark.asyncio
    async def test_the_message_is_never_the_generic_one(self):
        # The string this whole file exists to remove. Asserted on the
        # guard rather than on one caller, because the next path added
        # should fail here if it forgets to ask.
        reason = _controller(NoModelConfigured.for_login_required())._no_model()

        assert reason and "Unknown model error" not in reason
