"""An accuracy eval that returns no score says so, instead of leaking a TypeError.

`AccuracyDriver.run` guarded `result is None` and then read
`result.avg_score`, which agno types `float | None`. An eval that produced
no scored iterations therefore reached `None >= threshold`.

**This was not a crash.** The whole body sits inside
`except Exception as exc: return CheckResult(ok=False, detail=f"accuracy
eval error: {exc}")`, so the outcome was already a clean failure — just
reported as:

    accuracy eval error: '>=' not supported between instances of 'NoneType' and 'float'

which reads as a bug in igni rather than an eval that scored nothing. The
fix makes it `accuracy eval returned no score` and keeps `ok=False` either
way, so this is a legibility change, not a behaviour one. Worth having
because that string is what a user debugging their own eval suite sees.

It surfaced from mypy, but only after the numpy 2.5 upgrade was worked
around: `type X = ...` in numpy's own stubs made mypy abort with "errors
prevented further checking", so the run checked **zero files** while still
exiting non-zero for a reason that named numpy. Restoring the 615-file
check found this on the next run — the argument for never leaving a type
checker in a state where it cannot report.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ember_code.core.evals.assertion_runner import AccuracyDriver, AssertionContext


def _ctx(*, threshold: float) -> AssertionContext:
    """Minimal context. `model_construct` skips validation so the stub
    objects below do not have to satisfy the real field types."""
    case = SimpleNamespace(
        input="q",
        expected_output="a",
        judge_guidelines=None,
        num_iterations=1,
        accuracy_threshold=threshold,
    )
    return AssertionContext.model_construct(
        case=case,
        output_text="whatever the agent said",
        judge_model=object(),
        agent=object(),
    )


class _FakeAccuracy:
    """Stands in for agno's `AccuracyEval`, returning a chosen result."""

    def __init__(self, result):
        self._result = result

    async def arun_with_output(self, **_kwargs):
        return self._result


@pytest.fixture
def patched(monkeypatch):
    """Swap the agno eval the driver constructs for a stub."""

    def _install(result):
        import ember_code.core.evals.assertion_runner as module

        monkeypatch.setattr(
            module,
            "AccuracyEval",
            lambda **_kwargs: _FakeAccuracy(result),
            raising=False,
        )

    return _install


class TestAMissingScore:
    async def test_the_detail_names_the_cause_not_the_operator(self, patched):
        patched(SimpleNamespace(avg_score=None, results=[]))

        check = await AccuracyDriver().run(_ctx(threshold=8.0))

        assert check.ok is False
        detail = (check.detail or "").lower()
        assert "no score" in detail, check.detail
        # The old message. Asserting its absence is the actual regression
        # guard: without the `is None` branch the except block produces it.
        assert "not supported between" not in detail, check.detail
        # And it stays distinguishable from the `result is None` case
        # immediately above it in the driver.
        assert detail != "accuracy eval returned none"


class TestAPresentScoreStillWorks:
    """The guard must not swallow the ordinary paths."""

    @pytest.mark.parametrize(
        "score,threshold,expected_ok",
        [(9.0, 8.0, True), (8.0, 8.0, True), (7.0, 8.0, False), (0.0, 0.0, True)],
    )
    async def test_it_compares_against_the_threshold(self, patched, score, threshold, expected_ok):
        patched(SimpleNamespace(avg_score=score, results=[]))

        check = await AccuracyDriver().run(_ctx(threshold=threshold))

        assert check.ok is expected_ok, (
            f"{score} >= {threshold} should be {expected_ok} (got {check.detail})"
        )
        assert check.score == score

    async def test_zero_is_a_score_and_not_a_missing_one(self, patched):
        """`if score is None` rather than `if not score` — 0.0 is a real
        result, and a falsy check would report it as no score at all."""
        patched(SimpleNamespace(avg_score=0.0, results=[]))

        check = await AccuracyDriver().run(_ctx(threshold=5.0))

        assert check.ok is False
        assert "no score" not in (check.detail or "").lower(), check.detail
        assert check.score == 0.0
