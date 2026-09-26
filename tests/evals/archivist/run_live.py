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


def build_settings():
    """The user's real model registry, with sampling pinned off."""
    from ember_code.core.config.settings import load_settings

    settings = load_settings()
    entry = settings.models.registry.get(settings.models.default)
    if entry is None:
        sys.exit(
            f"models.default is {settings.models.default!r}, which is not in "
            "models.registry — run /login or fix ~/.igni/config.yaml"
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


async def run_one_case(agent, settings, case):
    from ember_code.core.agents.pool import AgentPool
    from ember_code.core.evals.runner import SuiteRunner
    from ember_code.core.evals.schemas import EvalSuite

    suite = EvalSuite(
        agent=AGENT_NAME,
        description=f"live one-case: {case.name}",
        setup_module=SETUP_MODULE,
        cases=[case],
    )
    pool = AgentPool()
    pool._agents[AGENT_NAME] = agent  # type: ignore[attr-defined]

    runner = SuiteRunner(
        suite=suite, pool=pool, settings=settings, project_dir=REPO_ROOT
    )
    result = (await runner.run()).case_results[0]
    return {
        "name": case.name,
        "passed": result.passed,
        "tools": [c.get("name", "?") for c in result.tool_trace],
        "elapsed": result.elapsed,
        "error": result.error,
        "judge": getattr(result, "judge_reasoning", "") or "",
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="run one case by name")
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    args = parser.parse_args()

    settings = build_settings()
    suite = load_eval_file(EVAL_FILE)

    cases = suite.cases
    if args.case:
        cases = [c for c in cases if c.name == args.case]
        if not cases:
            sys.exit(f"no case named {args.case!r}")
    if args.limit:
        cases = cases[: args.limit]

    print(f"model: {settings.models.default}  |  cases: {len(cases)}\n")

    results = []
    for case in cases:
        agent, _ = await build_agent(settings)
        try:
            result = await run_one_case(agent, settings, case)
        except Exception as exc:  # a driver failure is not a case failure
            result = {
                "name": case.name,
                "passed": False,
                "tools": [],
                "elapsed": 0.0,
                "error": f"driver: {exc}",
                "judge": "",
            }
        results.append(result)
        mark = "PASS" if result["passed"] else "FAIL"
        print(f"  {mark}  {result['name']}  ({result['elapsed']:.1f}s)")
        if result["tools"]:
            print(f"        tools: {', '.join(result['tools'])}")
        if result["error"]:
            print(f"        error: {result['error'][:300]}")
        elif not result["passed"] and result["judge"]:
            print(f"        judge: {result['judge'][:400]}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
