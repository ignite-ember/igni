"""``host.decide``: an extension asks igni's decision model typed questions.

Laya runs in igni's decisions service, shared with the Bypass guard,
which goes first (``ember_code.decisions_server``); the backend asks
it over HTTP (``ember_code.core.decisions.laya``). These drive the
member through the host model, through the real client and the real
service app, over a stand-in router — what an extension sends, what
reaches Laya, what comes back, and what is refused — and, when the
model is on this machine, the real thing in its own process.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from ember_code.core.decisions import Laya, Priority
from ember_code.core.decisions.laya import RemoteDecisions
from ember_code.core.decisions.schemas import DecisionRequest
from ember_code.core.hooks.bypass_guard import QUESTIONS, HarmJudge
from ember_code.decisions_server.app import DecisionsApp
from ember_code.decisions_server.model import BUNDLE_REPO, HubCache, LayaModel, PriorityGate
from tests.host_model_helpers import HostRig

TRIAGE = [
    {
        "id": "kind",
        "kind": "choice",
        "question": "What kind of message is `text`?",
        "options": {"bug": "a bug report", "feature": "a feature request", "question": ""},
    },
    {"id": "urgent", "kind": "noul", "question": "Is `text` urgent?"},
    {
        "id": "severity",
        "kind": "score",
        "question": "How severe is `text`?",
        "levels": ["none", "low", "high", "critical"],
    },
]


class FakeRouter:
    """Laya's router, answering from a script and recording what it was asked."""

    def __init__(self, *, truncated: bool = False, fail: Exception | None = None) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.single: list[tuple[dict[str, Any], dict[str, Any], str | None]] = []
        self.truncated = truncated
        self.fail = fail

    def predict_batch(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.fail is not None:
            raise self.fail
        self.batches.append(requests)
        return [self._answer(r["questions"]) for r in requests]

    def predict(
        self, state: dict[str, Any], questions: dict[str, Any], model: str | None = None
    ) -> dict[str, Any]:
        self.single.append((state, questions, model))
        return self._answer(questions)

    def _answer(self, questions: dict[str, Any]) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for qid, q in questions.items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.8}
            elif q["type"] == "score":
                n = len(q["criteria"])
                probabilities = {str(i): (0.7 if i == n - 1 else 0.3 / (n - 1)) for i in range(n)}
                answers[qid] = {"type": "score", "score": n - 1.3, "probabilities": probabilities}
            else:
                labels = list(q["criteria"])
                probabilities = {
                    label: (0.9 if i == 0 else 0.1 / (len(labels) - 1))
                    for i, label in enumerate(labels)
                }
                answers[qid] = {
                    "type": "choice",
                    "choice": labels[0],
                    "probabilities": probabilities,
                }
        return {
            "answers": answers,
            "usage": {
                "truncated": self.truncated,
                "state_tokens_dropped": 40 if self.truncated else 0,
                "truncated_questions": [],
            },
            "routing": {"model": "english"},
        }


def service_over(model: LayaModel) -> Laya:
    """The backend's client, talking to the real service app in-process."""
    app = DecisionsApp(model, "secret")
    transport = httpx.ASGITransport(app=app.asgi)
    return Laya(RemoteDecisions("http://decisions", "secret", transport=transport))


def laya_over(router: Any) -> Laya:
    model = LayaModel()
    model._router = router  # noqa: SLF001 — the stand-in for a loaded checkpoint
    return service_over(model)


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def ok(reply: Any) -> Any:
    assert reply.ok, f"refused: {reply.error}"
    return reply.result


def refused(reply: Any, code: str) -> str:
    assert not reply.ok, f"answered {reply.result!r}"
    assert reply.error.code == code, reply.error
    return reply.error.message


class TestDecide:
    async def test_each_kind_comes_back_as_an_answer_with_its_probabilities(
        self, rig: HostRig
    ) -> None:
        router = FakeRouter()
        assert rig.model is not None
        rig.model.decisions = laya_over(router)
        ext = rig.install(host=["decide"])

        decision = ok(
            await rig.ask(
                ext, "call", "decide", context={"text": "It crashes on start"}, questions=TRIAGE
            )
        )

        assert decision["model"] == "english"
        kind, urgent, severity = (decision["answers"][q] for q in ("kind", "urgent", "severity"))
        assert kind["answer"] == "bug" and kind["confidence"] == pytest.approx(0.9)
        assert set(kind["probabilities"]) == {"bug", "feature", "question"}
        assert urgent["answer"] == "yes"
        assert urgent["probabilities"] == pytest.approx({"yes": 0.8, "no": 0.2})
        assert severity["answer"] == "critical" and severity["score"] == pytest.approx(2.7)
        assert list(severity["probabilities"]) == ["none", "low", "high", "critical"]

    async def test_laya_is_asked_in_its_own_shape_on_its_general_checkpoints(
        self, rig: HostRig
    ) -> None:
        router = FakeRouter()
        assert rig.model is not None
        rig.model.decisions = laya_over(router)
        ext = rig.install(host=["decide"])

        ok(await rig.ask(ext, "call", "decide", context={"text": "x"}, questions=TRIAGE))

        [[request]] = router.batches
        assert request["state"] == {"text": "x"}
        assert "model" not in request  # Laya routes: english or multilingual
        assert request["questions"]["kind"] == {
            "type": "choice",
            "instructions": "What kind of message is `text`?",
            "criteria": {"bug": "a bug report", "feature": "a feature request", "question": None},
        }
        assert request["questions"]["severity"]["criteria"] == ["none", "low", "high", "critical"]
        assert request["questions"]["urgent"] == {
            "type": "noul",
            "instructions": "Is `text` urgent?",
        }

    async def test_a_batch_answers_each_context_in_order(self, rig: HostRig) -> None:
        router = FakeRouter()
        assert rig.model is not None
        rig.model.decisions = laya_over(router)
        ext = rig.install(host=["decide"])

        decisions = ok(
            await rig.ask(
                ext,
                "call",
                "decide_batch",
                contexts=[{"text": "a"}, {"text": "b"}, {"text": "c"}],
                questions=TRIAGE[1:2],
            )
        )

        assert len(decisions) == 3
        assert [r["state"]["text"] for r in router.batches[0]] == ["a", "b", "c"]

    async def test_it_needs_the_decide_grant(self, rig: HostRig) -> None:
        assert rig.model is not None
        rig.model.decisions = laya_over(FakeRouter())
        ext = rig.install(host=["app"])
        refused(
            await rig.ask(ext, "call", "decide", context={"text": "x"}, questions=TRIAGE),
            "not_granted",
        )

    async def test_a_context_laya_would_cut_is_refused_not_answered_from_part(
        self, rig: HostRig
    ) -> None:
        assert rig.model is not None
        rig.model.decisions = laya_over(FakeRouter(truncated=True))
        ext = rig.install(host=["decide"])
        message = refused(
            await rig.ask(ext, "call", "decide", context={"text": "x"}, questions=TRIAGE),
            "invalid",
        )
        assert "40 tokens" in message

    async def test_a_model_that_will_not_load_is_unavailable(self, rig: HostRig) -> None:
        model = LayaModel()

        def broken() -> Any:
            raise OSError("no weights")

        model._build = broken  # type: ignore[method-assign]
        assert rig.model is not None
        rig.model.decisions = service_over(model)
        ext = rig.install(host=["decide"])
        refused(
            await rig.ask(ext, "call", "decide", context={"text": "x"}, questions=TRIAGE),
            "unavailable",
        )

    @pytest.mark.parametrize(
        ("context", "questions", "why"),
        [
            ({}, TRIAGE, "named fields"),
            ({"Text": "x"}, TRIAGE, "lowercase name"),
            ({"text": "x" * 6001}, TRIAGE, "6000 characters"),
            ({f"f{i}": "x" for i in range(9)}, TRIAGE, "named fields"),
            ({"text": "x"}, [], "at least 1"),
            ({"text": "x"}, [TRIAGE[1] | {"id": f"q{i}"} for i in range(17)], "at most 16"),
            ({"text": "x"}, [TRIAGE[1], TRIAGE[1]], "its own id"),
            ({"text": "x"}, [TRIAGE[1] | {"kind": "maybe"}], "kind"),
            ({"text": "x"}, [TRIAGE[1] | {"question": "q" * 501}], "500 characters"),
            ({"text": "x"}, [TRIAGE[0] | {"options": {"only": ""}}], "2 to 20 options"),
            (
                {"text": "x"},
                [TRIAGE[0] | {"options": {f"o{i}": "" for i in range(21)}}],
                "2 to 20 options",
            ),
            ({"text": "x"}, [TRIAGE[0] | {"options": {"a": "", "b": "d" * 201}}], "description"),
            ({"text": "x"}, [TRIAGE[0] | {"options": {"a": "", " ": ""}}], "label"),
            ({"text": "x"}, [TRIAGE[2] | {"levels": ["low", "low"]}], "named differently"),
            ({"text": "x"}, [TRIAGE[2] | {"levels": [str(i) for i in range(11)]}], "2 to 10"),
            ({"text": "x"}, [TRIAGE[1] | {"levels": ["a", "b"]}], "no options or levels"),
            ({"text": "x"}, [TRIAGE[2] | {"options": {"a": "", "b": ""}}], "levels, not options"),
        ],
    )
    async def test_what_does_not_fit_is_refused_before_laya_is_asked(
        self, rig: HostRig, context: dict[str, str], questions: list[dict[str, Any]], why: str
    ) -> None:
        router = FakeRouter()
        assert rig.model is not None
        rig.model.decisions = laya_over(router)
        ext = rig.install(host=["decide"])
        message = refused(
            await rig.ask(ext, "call", "decide", context=context, questions=questions), "invalid"
        )
        assert why in message
        assert router.batches == []

    async def test_more_than_sixteen_contexts_is_refused(self, rig: HostRig) -> None:
        assert rig.model is not None
        rig.model.decisions = laya_over(FakeRouter())
        ext = rig.install(host=["decide"])
        refused(
            await rig.ask(
                ext,
                "call",
                "decide_batch",
                contexts=[{"text": str(i)} for i in range(17)],
                questions=TRIAGE,
            ),
            "invalid",
        )


class TestSharedWithTheBypassGuard:
    def test_the_guard_asks_its_measured_checkpoint_first(self) -> None:
        router = FakeRouter()
        judge = HarmJudge(laya=laya_over(router))
        probability, which = asyncio.run(judge.score("Bash: rm -rf ~"))
        [(state, questions, model)] = router.single
        assert state == {"tool_call": "Bash: rm -rf ~"}
        assert questions == QUESTIONS  # sent over the wire, unchanged
        assert model == "typed-decisions"
        assert which in QUESTIONS and 0.0 <= probability <= 1.0

    def test_an_approval_waiting_goes_before_extensions_waiting(self) -> None:
        gate = PriorityGate()
        order: list[str] = []
        holding = threading.Event()
        release = threading.Event()

        def first() -> None:
            with gate.hold(Priority.EXTENSION):
                holding.set()
                release.wait()

        def waiter(name: str, priority: Priority) -> None:
            with gate.hold(priority):
                order.append(name)

        threads = [threading.Thread(target=first)]
        threads[0].start()
        holding.wait()
        for name, priority in (
            ("extension-1", Priority.EXTENSION),
            ("extension-2", Priority.EXTENSION),
            ("approval", Priority.APPROVAL),
        ):
            t = threading.Thread(target=waiter, args=(name, priority))
            t.start()
            threads.append(t)
            time.sleep(0.05)
        release.set()
        for t in threads:
            t.join(5)
        assert order == ["approval", "extension-1", "extension-2"]


@pytest.mark.skipif(
    not HubCache.has(BUNDLE_REPO) and os.environ.get("IGNI_TEST_LAYA") != "1",
    reason="Laya is not on this machine (set IGNI_TEST_LAYA=1 to download it)",
)
def test_the_real_model_tells_a_bug_from_a_feature_request() -> None:
    laya = Laya()
    request = DecisionRequest(
        contexts=[
            {"text": "The app crashes on start for every user since the update"},
            {"text": "Could you add a dark mode to the settings page?"},
        ],
        questions=TRIAGE[:1],
    )
    bug, feature = asyncio.run(laya.decide(request))
    assert bug.answers["kind"].answer == "bug"
    assert feature.answers["kind"].answer == "feature"
    assert bug.model in ("english", "multilingual")
