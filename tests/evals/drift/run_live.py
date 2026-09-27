"""Grade the drift update against a live model.

Not a pytest test — it costs a model call per case and needs
credentials, so it is a driver you invoke deliberately:

    .venv/bin/python tests/evals/drift/run_live.py            # all
    .venv/bin/python tests/evals/drift/run_live.py --case NAME
    .venv/bin/python tests/evals/drift/run_live.py --model X

This grades the *update*, not the agent: ``apply_drift`` is a one-shot
model call with no tools, so there are no tool calls to check and no
transcript to judge. What there is instead is a document, and a
document can be graded against facts.

Scored on three lists rather than by a judge:

``removed``    statements the upstream change made false. One that
               survives is a fact the update missed — which is the
               question the suite exists to answer.
``present``    what it should say instead.
``preserved``  passages the change does not touch. These catch the
               opposite failure: a rewrite that fixes the document by
               replacing it, losing everything else it said.

Temperature is pinned to 0 for the reason the archivist suite
documents: a score that moves on its own cannot tell you whether a
prompt change helped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from ember_code.backend.server_artifacts import _strip_fence  # noqa: E402
from ember_code.core.artifacts.drift import Divergence, DriftUpdate  # noqa: E402
from ember_code.core.artifacts.side_by_side import RowKind  # noqa: E402
from ember_code.core.artifacts.side_by_side import build as build_side_by_side  # noqa: E402

EVAL_FILE = REPO_ROOT / "evals" / "drift.yaml"


@dataclass
class Score:
    """What one case produced, and where it fell short."""

    name: str
    missed: list[str]
    absent: list[str]
    lost: list[str]
    output: str
    error: str = ""

    @property
    def reached(self) -> bool:
        """Whether the model actually answered.

        Kept apart from ``passed`` because they mean different things
        to whoever is reading the score. A run that never reached the
        model is not a failing update — it is no result at all, and
        reporting it as FAIL reads as a regression in the prompt.
        Seen for real: an exhausted token plan produced three empty
        answers and a tidy ``0/3``.
        """
        return not self.error and bool(self.output.strip())

    @property
    def passed(self) -> bool:
        return self.reached and not (self.missed or self.absent or self.lost)


def build_settings(model: str | None = None):
    """The user's real model registry, with sampling pinned off.

    ``IGNI_EVAL_API_KEY`` overrides the chosen entry's key for this
    process only. Which credential has credit today is a fact about
    today, not about the product, so it is an environment variable
    rather than an edit to ``~/.igni/config.yaml`` — which keeps its
    own key, and never learns about this one.
    """
    import os

    from ember_code.core.config.settings import load_settings

    settings = load_settings()
    if model:
        settings.models.default = model
    entry = settings.models.registry.get(settings.models.default)
    if entry is None:
        sys.exit(
            f"model {settings.models.default!r} is not in models.registry "
            f"— known: {', '.join(sorted(settings.models.registry))}"
        )
    override = os.environ.get("IGNI_EVAL_API_KEY", "").strip()
    if override:
        entry.api_key = override
    entry.temperature = 0.0
    return settings


def divergence_for(case: dict) -> Divergence:
    """The case as the update sees it, built the way the backend does."""
    before = case["source_before"]
    after = case["source_after"]
    diff = build_side_by_side(before, after, from_n=1, to_n=2)
    return Divergence(
        dependent_id="doc",
        dependent_name=case["document_name"],
        dependent_n=1,
        source_id="src",
        source_name=case["source_name"],
        pinned_n=1,
        source_head_n=2,
        changes=[
            (row.kind.value, row.left, row.right)
            for row in diff.rows
            if row.kind is not RowKind.EQUAL
        ],
        dependent_content=case["document"],
        source_head_content=after,
    )


def _flat(text: str) -> str:
    """Lower-cased with runs of whitespace collapsed.

    Both halves earn their place. Case, because this is about facts
    and not spelling. Whitespace, because prose wraps: a phrase like
    "burst allowance of 100" sits across a line break in the source
    document and will wrap somewhere else in the model's answer. Left
    unflattened, such a check matches neither side and therefore never
    fails — which reads as coverage while providing none.
    """
    return " ".join(text.lower().split())


def grade(case: dict, output: str) -> Score:
    """Score one result against the case's three lists."""
    expect = case.get("expect", {})
    haystack = _flat(output)
    return Score(
        name=case["name"],
        missed=[s for s in expect.get("removed", []) if _flat(s) in haystack],
        absent=[s for s in expect.get("present", []) if _flat(s) not in haystack],
        lost=[s for s in expect.get("preserved", []) if _flat(s) not in haystack],
        output=output,
    )


async def run_case(case: dict, settings) -> Score:
    from agno.agent import Agent

    from ember_code.core.config.models import ModelRegistry

    try:
        agent = Agent(
            name="_drift_eval",
            model=ModelRegistry(settings).get_model(),
            markdown=False,
        )
        response = await agent.arun(DriftUpdate.prompt_for(divergence_for(case)))
    except Exception as exc:  # noqa: BLE001 — a model call, many failure modes
        return Score(name=case["name"], missed=[], absent=[], lost=[], output="", error=str(exc))

    # Agno does not raise on a provider failure: it logs, sets
    # ``status`` to ERROR, and puts the *error message* in ``content``.
    # So the ``except`` above never fires and, read naively, the run
    # looks like an answer. An exhausted token plan graded as three
    # wrong documents and printed a tidy ``0/3`` before this check
    # existed.
    failed = str(getattr(getattr(response, "status", ""), "value", "")).upper() == "ERROR"
    answer = _strip_fence(str(getattr(response, "content", "") or ""))
    if failed or not answer.strip():
        return Score(
            name=case["name"],
            missed=[],
            absent=[],
            lost=[],
            output="",
            error=answer.strip() or "the model returned nothing — check credentials and quota",
        )
    return grade(case, answer)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="run one case by name")
    parser.add_argument("--model", help="override models.default for this run")
    parser.add_argument("--show", action="store_true", help="print each updated document")
    args = parser.parse_args()

    cases = yaml.safe_load(EVAL_FILE.read_text())["cases"]
    if args.case:
        cases = [c for c in cases if c["name"] == args.case]
        if not cases:
            sys.exit(f"no case named {args.case!r}")

    settings = build_settings(args.model)
    print(f"model: {settings.models.default}  cases: {len(cases)}\n")

    scores = [await run_case(case, settings) for case in cases]
    for score in scores:
        verdict = "PASS" if score.passed else "FAIL" if score.reached else "ERROR"
        print(f"{verdict}  {score.name}")
        if score.error:
            print(f"      {score.error}")
        # Named individually, because "3 of 4" is the result that
        # matters and a count would hide which one got away.
        for stale in score.missed:
            print(f"      still says: {stale!r}")
        for missing in score.absent:
            print(f"      never says: {missing!r}")
        for lost in score.lost:
            print(f"      lost: {lost!r}")
        if args.show and score.output:
            print("      ── output ──")
            for line in score.output.splitlines():
                print(f"      {line}")
        print()

    passed = sum(1 for s in scores if s.passed)
    unreached = [s for s in scores if not s.reached]
    graded = len(scores) - len(unreached)

    # Scored out of what was actually graded, and the rest named
    # rather than folded in. "0/3" when the model was never reached is
    # a number that means nothing and looks like everything.
    print(f"{passed}/{graded} passed" if graded else "nothing was graded")
    if unreached:
        print(
            f"{len(unreached)} case(s) never reached the model: "
            + ", ".join(s.name for s in unreached)
        )
        return 2
    return 0 if passed == graded else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
