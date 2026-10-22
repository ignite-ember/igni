"""Run the archivist suite against a live model.

Not a pytest test — it costs a model call per case and needs
credentials, so it is a driver you invoke deliberately:

    .venv/bin/python tests/evals/archivist/run_live.py            # all
    .venv/bin/python tests/evals/archivist/run_live.py --case NAME
    .venv/bin/python tests/evals/archivist/run_live.py --limit 3

Model config comes from the user's own ``~/.igni/config.yaml`` rather
than from env vars, so this grades the archivist against the model the
product would actually use. Temperature is pinned to 0: the
``data-architect`` suite's pass rate swung between 4/12 and 8/12 on
identical input before that was fixed, and a suite whose score moves
on its own cannot tell you whether a prompt change helped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ember_code.core.evals.loader import load_eval_file  # noqa: E402

AGENT_NAME = "archivist"
EVAL_FILE = REPO_ROOT / "evals" / "archivist.yaml"
AGENT_FILE = REPO_ROOT / "src" / "ember_code" / "bundled_agents" / "archivist.md"
SETUP_MODULE = "evals.archivist.setup"


def build_settings(model: str | None = None):
    """The user's real model registry, with sampling pinned off.

    ``model`` overrides ``models.default`` for this run only. The
    registry holds several entries for the same model — a direct one
    and one routed through the capture proxy — and which of them has
    live credit is a fact about today, not about the product. So it is
    a flag rather than an edit to ``~/.igni/config.yaml``, which must
    keep its own default.
    """
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
    entry.temperature = 0.0
    return settings


async def build_agent(settings):
    from ember_code.core.agents.builder import AgentBuilder
    from ember_code.core.agents.markdown import AgentMarkdownFile
    from ember_code.core.agents.schemas import AgentBuildContext
    from ember_code.core.artifacts.index import ArtifactIndex
    from ember_code.core.session.artifact_ops import SessionArtifactManager
    from evals.archivist.setup import build_seeded_index

    index: ArtifactIndex = await build_seeded_index(REPO_ROOT)
    manager = SessionArtifactManager(index, settings, REPO_ROOT, session_id="eval")

    definition = AgentMarkdownFile(AGENT_FILE).parse()
    context = AgentBuildContext(
        settings=settings,
        base_dir=str(REPO_ROOT),
        artifact_mgr=manager,
    )
    return AgentBuilder(context).build(definition), index


async def run_one_case(agent, settings, case, allow_additional: bool = False):
    from ember_code.core.agents.pool import AgentPool
    from ember_code.core.evals.runner import SuiteRunner
    from ember_code.core.evals.schemas import EvalSuite

    suite = EvalSuite(
        agent=AGENT_NAME,
        description=f"live one-case: {case.name}",
        setup_module=SETUP_MODULE,
        allow_additional_tool_calls=allow_additional,
        cases=[case],
    )
    pool = AgentPool()
    pool._agents[AGENT_NAME] = agent  # type: ignore[attr-defined]

    runner = SuiteRunner(suite=suite, pool=pool, settings=settings, project_dir=REPO_ROOT)
    r = (await runner.run()).case_results[0]

    # Which dimension failed is the whole value of a red case: "the
    # agent reasoned wrongly" and "the agent never called the tool"
    # are different bugs and a bare FAIL does not separate them.
    dims = {
        "accuracy": (r.accuracy_passed, r.accuracy_reason),
        "expected_tools": (r.reliability_passed, r.reliability_detail),
        "unexpected_tools": (r.unexpected_passed, r.unexpected_detail),
        "tool_args": (r.tool_arg_passed, r.tool_arg_detail),
        "cypher": (r.cypher_passed, r.cypher_detail),
    }
    return {
        "name": case.name,
        "passed": r.passed,
        "tools": [c.name for c in r.tool_trace],
        "elapsed": r.elapsed,
        "error": r.error,
        "failed_dims": {k: (detail or "") for k, (ok, detail) in dims.items() if ok is False},
        "response": r.response_text or "",
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", help="run one case by name (repeatable)")
    parser.add_argument("--show-response", action="store_true", help="print the agent's answer")
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    parser.add_argument(
        "--model", help="override models.default for this run (e.g. MiniMax-M2.7-proxied)"
    )
    args = parser.parse_args()

    settings = build_settings(args.model)
    suite = load_eval_file(EVAL_FILE)

    cases = suite.cases
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c.name in wanted]
        missing = wanted - {c.name for c in cases}
        if missing:
            sys.exit(f"no case named: {', '.join(sorted(missing))}")
    if args.limit:
        cases = cases[: args.limit]

    print(f"model: {settings.models.default}  |  cases: {len(cases)}\n")

    results = []
    for case in cases:
        agent, _ = await build_agent(settings)
        try:
            result = await run_one_case(agent, settings, case, suite.allow_additional_tool_calls)
        except Exception as exc:  # a driver failure is not a case failure
            result = {
                "name": case.name,
                "passed": False,
                "tools": [],
                "elapsed": 0.0,
                "error": f"driver: {exc}",
                "failed_dims": {},
                "response": "",
            }
        results.append(result)
        mark = "PASS" if result["passed"] else "FAIL"
        print(f"  {mark}  {result['name']}  ({result['elapsed']:.1f}s)")
        if result["tools"]:
            print(f"        tools: {', '.join(result['tools'])}")
        if result["error"]:
            print(f"        error: {result['error'][:300]}")
        for dim, detail in result["failed_dims"].items():
            print(f"        {dim}: {detail[:400]}")
        if args.show_response and result["response"]:
            body = "\n".join("        | " + ln for ln in result["response"].splitlines())
            print(body[:2500])

    passed = sum(1 for r in results if r["passed"])
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
