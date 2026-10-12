"""The Bypass safety check says what *this* call does, not a stock phrase.

A rule carried one hand-written reason ("force-pushes, rewriting shared
history") and Laya only a category, so a command nobody wrote a rule
for got "might delete or overwrite data" and nothing more. Once a call
is stopped, the session's model now describes it, and the sentence
replaces the stock text in the open dialog.

The explainer never decides — the call is already paused when it runs —
and any failure keeps the stock text.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ember_code.backend.hitl_controller import HitlController
from ember_code.backend.hitl_stream_mux import HITLStreamMultiplexer
from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.core.config.schemas.safety import BypassGuardConfig
from ember_code.core.hooks.bypass_explainer import CallExplainer, clean
from ember_code.core.hooks.bypass_guard import BypassGuard, Verdict, explained
from ember_code.core.session.core import Session
from ember_code.core.sub_agent_hitl import SubAgentHITLCoordinator
from ember_code.protocol import messages as msg

_SENTENCE = (
    "Overwrites main on origin with your local branch, discarding any commits "
    "others pushed since you last pulled"
)


class FakeExplainer:
    def __init__(self, sentence: str | None = _SENTENCE, delay: float = 0.0):
        self.sentence, self.delay, self.calls = sentence, delay, []

    async def explain(self, tool_name, args, flagged_for):
        self.calls.append((tool_name, args, flagged_for))
        await asyncio.sleep(self.delay)
        return self.sentence


class NoJudge:
    async def score(self, text):
        return 0.0, "destructive"


def _guard(project: Path, *, ask, explainer=None) -> BypassGuard:
    return BypassGuard(
        config=BypassGuardConfig(),
        project_dir=project,
        in_bypass=lambda: True,
        ask=ask,
        judge=NoJudge(),
        explainer=explainer,
    )


# ── what the model's answer becomes ─────────────────────────────────


class TestTheAnswer:
    def test_thinking_quotes_and_spacing_are_dropped(self):
        assert clean('<think>it is a push</think>\n "Pushes  main."\n') == "Pushes main."

    def test_code_quotes_are_dropped(self):
        assert clean("Force-pushes `main` to `origin`.") == "Force-pushes main to origin."

    def test_an_unclosed_think_block_leaves_nothing(self):
        assert clean("<think>still going") is None

    def test_an_empty_or_rambling_answer_is_not_used(self):
        assert clean("   ") is None
        assert clean("word " * 200) is None

    def test_the_sentence_is_followed_by_what_stopped_the_call(self):
        rule = Verdict(needs_approval=True, reason="force-pushes, rewriting shared history")
        assert explained(rule, _SENTENCE) == (
            f"{_SENTENCE}. Bypass paused here because a safety rule matched "
            "(force-pushes, rewriting shared history)."
        )
        judged = Verdict(
            needs_approval=True, reason="might delete or overwrite data", score=0.91, judged=True
        )
        assert "the safety model rated it 0.91 of 1 (might delete or overwrite data)" in (
            explained(judged, "Deletes the build cache.")
        )
        unchecked = Verdict(needs_approval=True, reason="could not be checked for safety")
        assert explained(unchecked, "Runs a script.").endswith(
            "because the call could not be checked for safety."
        )


class TestTheQuestionPutToTheModel:
    def test_the_call_is_fenced_off_as_data(self, tmp_path):
        prompt = CallExplainer(lambda: None, project_dir=tmp_path).prompt_for(
            "run_shell_command",
            {"command": "git push --force origin main"},
            "force-pushes, rewriting shared history",
        )
        assert f"Project directory: {tmp_path}" in prompt
        assert "Flagged because it force-pushes" in prompt
        call = prompt.split("<tool_call>")[1].split("</tool_call>")[0]
        assert "git push --force origin main" in call

    def test_a_huge_write_is_cut_down(self):
        prompt = CallExplainer(lambda: None).prompt_for(
            "write_file", {"path": "x", "content": "a" * 50_000}, "might overwrite data"
        )
        assert len(prompt) < 5_000 and "(truncated)" in prompt


class TestNoSentenceKeepsTheStockText:
    def test_no_model_configured(self):
        from ember_code.core.config.null_model import NoModelConfigured

        explainer = CallExplainer(lambda: NoModelConfigured(id="none"))
        assert asyncio.run(explainer.explain("bash", {"command": "x"}, "why")) is None

    def test_a_model_that_raises(self):
        def broken():
            raise RuntimeError("provider down")

        assert asyncio.run(CallExplainer(broken).explain("bash", {}, "why")) is None

    def test_a_model_that_is_too_slow(self, monkeypatch):
        explainer = CallExplainer(lambda: None, timeout=0.05)

        async def slow(*_):
            await asyncio.sleep(5)

        monkeypatch.setattr(explainer, "_ask", slow)
        assert asyncio.run(explainer.explain("bash", {}, "why")) is None


# ── the guard hands the sentence over ───────────────────────────────


class Asker:
    def __init__(self):
        self.reason = self.follow_up = None

    async def __call__(self, tool_name, args, reason, agent_path, *, follow_up=None):
        self.reason, self.follow_up = reason, follow_up
        return True


def test_the_guard_asks_at_once_and_explains_after(tmp_path):
    ask, explainer = Asker(), FakeExplainer()
    asyncio.run(
        _guard(tmp_path, ask=ask, explainer=explainer).check(
            "run_shell_command", {"command": "git push --force origin main"}
        )
    )
    assert ask.reason.startswith("This command force-pushes"), "the stock text goes up first"
    assert explainer.calls == [], "the model is not waited on before asking"
    better = asyncio.run(ask.follow_up())
    assert better.startswith(_SENTENCE) and "a safety rule matched" in better
    assert explainer.calls[0][2] == "force-pushes, rewriting shared history"


def test_no_explainer_no_follow_up(tmp_path):
    ask = Asker()
    asyncio.run(_guard(tmp_path, ask=ask).check("run_shell_command", {"command": "rm -rf ~"}))
    assert ask.follow_up is None


def test_a_failed_explanation_changes_nothing(tmp_path):
    ask = Asker()
    asyncio.run(
        _guard(tmp_path, ask=ask, explainer=FakeExplainer(None)).check(
            "run_shell_command", {"command": "rm -rf ~"}
        )
    )
    assert asyncio.run(ask.follow_up()) is None


# ── the coordinator ─────────────────────────────────────────────────


class TestTheCoordinator:
    def _pending(self, coordinator, *, surfaced):
        req = SimpleNamespace(details="stock", confirm=lambda: None, reject=lambda note="": None)
        req_id = asyncio.run(coordinator.push_requirement(req, run_id="r"))
        if surfaced:
            coordinator.list_new_pending()
        return req_id, req

    def test_an_open_question_on_screen_gets_an_update(self):
        coordinator = SubAgentHITLCoordinator()
        req_id, req = self._pending(coordinator, surfaced=True)
        coordinator.update_details(req_id, "better")
        assert req.details == "better"
        assert coordinator.new_arrival.is_set()
        assert coordinator.take_detail_updates() == [(req_id, "better")]
        assert coordinator.take_detail_updates() == []

    def test_one_not_yet_shown_goes_out_with_the_new_text(self):
        coordinator = SubAgentHITLCoordinator()
        req_id, req = self._pending(coordinator, surfaced=False)
        coordinator.update_details(req_id, "better")
        assert req.details == "better"
        assert coordinator.take_detail_updates() == []

    def test_an_answered_question_is_left_alone(self):
        coordinator = SubAgentHITLCoordinator()
        req_id, req = self._pending(coordinator, surfaced=True)
        coordinator.resolve(req_id, "confirm")
        coordinator.update_details(req_id, "better")
        assert req.details == "stock" and coordinator.take_detail_updates() == []


# ── end to end: the open dialog's text is replaced ──────────────────


@pytest.mark.parametrize("delay", [0.0, 0.2])
def test_the_open_dialog_receives_the_explanation(tmp_path, delay):
    """The dialog goes up with the stock text; the model's sentence
    follows as ``requirement_details`` for the same requirement. With
    no delay the explanation can land before the relay surfaces the
    question — then it simply goes out with it."""

    async def scenario():
        session = SimpleNamespace(sub_agent_hitl=SubAgentHITLCoordinator(), project_dir=tmp_path)
        guard = _guard(
            tmp_path,
            ask=lambda *a, **kw: Session._ask_about_tool_call(session, *a, **kw),
            explainer=FakeExplainer(delay=delay),
        )
        outcome = {}

        async def team_stream():
            outcome["block"] = await guard.check(
                "run_shell_command", {"command": "git push --force origin main"}, ["main"]
            )
            if False:
                yield None

        store = PendingRequirementsStore()
        mux = HITLStreamMultiplexer(
            session=session,
            store=store,
            pause_handler=None,
            tracer=SimpleNamespace(trace=lambda *_: None),
        )
        controller = HitlController(session, store)
        shown: dict[str, str] = {}
        async for message in mux.stream(team_stream()):
            if isinstance(message, msg.RunPaused):
                req = message.requirements[0]
                shown[req.requirement_id] = req.details
                if _SENTENCE in req.details:
                    pending = req.requirement_id
                    break
            if isinstance(message, msg.RequirementDetails):
                assert message.requirement_id in shown, "an update for a dialog that is up"
                shown[message.requirement_id] = message.details
                pending = message.requirement_id
                break
        async for _ in controller.resolve_batch(
            [msg.HITLDecision(requirement_id=pending, action="confirm")]
        ):
            pass
        await asyncio.sleep(0.05)
        return shown[pending], outcome.get("block", "not finished")

    text, block = asyncio.run(asyncio.wait_for(scenario(), timeout=10))
    assert text.startswith(_SENTENCE)
    assert "a safety rule matched (force-pushes" in text
    assert block is None, "allowed, so the call runs"


def test_answering_first_stops_the_explainer(tmp_path):
    async def scenario():
        coordinator = SubAgentHITLCoordinator()
        coordinator.listeners = 1
        session = SimpleNamespace(sub_agent_hitl=coordinator)
        started = asyncio.Event()

        async def follow_up():
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(
            Session._ask_about_tool_call(
                session, "bash", {"command": "x"}, "stock", [], follow_up=follow_up
            )
        )
        await coordinator.new_arrival.wait()
        [(req_id, _)] = coordinator.list_new_pending()
        await started.wait()
        coordinator.resolve(req_id, "reject")
        return await asyncio.wait_for(task, timeout=2)

    assert asyncio.run(scenario()) is False
