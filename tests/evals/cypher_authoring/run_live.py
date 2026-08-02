"""Live-LLM runner for the data-architect Cypher authoring eval.

Drives the full pipeline (real model, real tool calls, real
Cypher) using credentials supplied via env vars. Designed to be
run by a developer, not in CI.

Usage:
    MINIMAX_API_KEY=sk-... MINIMAX_BASE_URL=https://api.minimax.io/v1 \\
        uv run python tests/evals/cypher_authoring/run_live.py

Note: this script resolves the repo root to ``/System/Volumes/Data/...``
because the test-sandbox shell sees the project at a different
path than the ``uv run`` subprocess does. On a normal dev
machine, the sandbox-less resolution at the top of the script
is the right one.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


# Sandbox-aware repo-root resolution. Try the unsandboxed path
# first (works on a normal dev machine); fall back to the
# system-volume path that the ``uv run`` subprocess actually
# sees.
def _resolve_repo_root() -> Path:
    candidates = [
        Path(__file__).resolve().parents[3],
        Path("/System/Volumes/Data")
        / Path(__file__).resolve().parents[3].relative_to(Path.cwd().anchor or "/"),
        Path("/System/Volumes/Data/Users/dmytrozezyk/ai_coding/ember-code"),
    ]
    for c in candidates:
        if (c / "evals" / "codeindex_architect_cypher.yaml").exists():
            return c
    return candidates[0]


REPO_ROOT = _resolve_repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ember_code.core.evals.loader import load_eval_file  # noqa: E402

AGENT_NAME = "data-architect"
EVAL_FILE = REPO_ROOT / "evals" / "codeindex_architect_cypher.yaml"
AGENT_FILE = REPO_ROOT / "agents" / "data-architect.codeindex.md"


def build_settings():
    from ember_code.core.config.model_entry import ModelRegistryEntry
    from ember_code.core.config.settings import Settings

    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        sys.exit("MINIMAX_API_KEY env var is required")
    base_url = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1")

    return Settings(
        models={
            "registry": {
                "MiniMax-M2.7": ModelRegistryEntry(
                    model_id="MiniMax-M2.7",
                    provider="openai_like",
                    url=base_url,
                    api_key=api_key,
                    # temperature=0 makes the eval deterministic
                    # — same input, same Cypher. Default sampling
                    # is what was making the pass rate fluctuate
                    # between 4/12 and 8/12 across runs.
                    temperature=0.0,
                ),
            },
            "default": "MiniMax-M2.7",
        }
    )


def build_agent(settings):
    from ember_code.core.agents.builder import AgentBuilder
    from ember_code.core.agents.markdown import AgentMarkdownFile
    from ember_code.core.agents.schemas import AgentBuildContext

    definition = AgentMarkdownFile(AGENT_FILE).parse()
    context = AgentBuildContext(settings=settings, base_dir=str(REPO_ROOT))
    return AgentBuilder(context).build(definition)


async def run_one_case(agent, settings, case):
    """Run a single case via the SuiteRunner.

    Builds an :class:`EvalSuite` model in memory with one case
    (avoids YAML round-trip — input may contain newlines that
    break the simple serializer used in the first cut of this
    driver). Passes ``project_dir=REPO_ROOT`` so the workspace's
    fixture resolution finds the existing ``evals/fixtures/``
    dataset; the workspace allocates its own tempdir as
    ``work_dir`` and tears it down after each case.
    """
    from ember_code.core.agents.pool import AgentPool
    from ember_code.core.evals.runner import SuiteRunner
    from ember_code.core.evals.schemas import EvalSuite

    suite = EvalSuite(
        agent=AGENT_NAME,
        description=f"live one-case: {case.name}",
        setup_module="evals.codeindex_architect_cypher.setup",
        cases=[case],
    )
    pool = AgentPool()
    pool._agents[AGENT_NAME] = agent  # type: ignore[attr-defined]

    runner = SuiteRunner(suite=suite, pool=pool, settings=settings, project_dir=REPO_ROOT)
    suite_result = await runner.run()
    case_result = suite_result.case_results[0]
    return {
        "name": case.name,
        "passed": case_result.passed,
        "cypher_passed": case_result.cypher_passed,
        "cypher_detail": case_result.cypher_detail,
        "tool_call_count": len(case_result.tool_trace),
        "elapsed": case_result.elapsed,
        "error": case_result.error,
    }


async def main():
    settings = build_settings()
    agent = build_agent(settings)

    suite = load_eval_file(EVAL_FILE)
    assert suite is not None, f"failed to load {EVAL_FILE}"

    print(
        f"=== Running {len(suite.cases)} cases on agent={AGENT_NAME} ===\n",
        flush=True,
    )

    results = []
    for case in suite.cases:
        try:
            r = await run_one_case(agent, settings, case)
        except Exception as exc:
            r = {
                "name": case.name,
                "passed": False,
                "error": f"runner crashed: {exc!r}",
            }
        results.append(r)
        status = "PASS" if r.get("passed") else "FAIL"
        detail = r.get("cypher_detail") or r.get("error", "") or ""
        print(
            f"  [{status}] {r['name']:<48} {r.get('elapsed', 0):5.1f}s  {detail[:80]}",
            flush=True,
        )

    n_pass = sum(1 for r in results if r.get("passed"))
    print(f"\n=== {n_pass} / {len(results)} passed ===", flush=True)
    sys.exit(0 if n_pass >= 8 else 1)


if __name__ == "__main__":
    asyncio.run(main())
