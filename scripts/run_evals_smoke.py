"""Smoke runner for the eval pilot.

Runs a configurable subset of cases from each suite against a live LLM,
reports pass/fail per case, then aggregate per agent + per category.

Usage:
    .venv/bin/python scripts/run_evals_smoke.py [--cases N] [--suite editor|main]

Requires EMBER_TEST_LLM_API_KEY (and optionally EMBER_TEST_LLM_MODEL /
EMBER_TEST_LLM_BASE_URL) — same env vars used by tests/test_live_agno_loops.py.
The same model serves as both the agent under test and the LLM judge for
AccuracyEval — keeps the smoke fast and self-contained.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

# Quiet down agno logs that flood the smoke output
logging.getLogger("agno").setLevel(logging.ERROR)
logging.getLogger("ember_code").setLevel(logging.WARNING)


def _install_websearch_stub() -> None:
    """Replace DuckDuckGoTools' search methods with deterministic stubs.

    Why: DDGS rate-limits/blocks our test IP and raises
    ``DDGSException: No results found`` on every call. The architect's
    eval cases that use ``WebSearch`` then spiral in an internal retry
    loop and burn the case_timeout — turning what should be a ~30s
    architecture-design case into a 270s ENV failure.

    The stub returns a short canned message synchronously; the agent
    sees "search returned nothing" without paying for retries, decides
    in-repo evidence is enough, and finishes in normal time. We're not
    testing DDGS here — we're testing whether the architect produces a
    sensible design, which is independent of web search availability.
    """
    try:
        from agno.tools.duckduckgo import DuckDuckGoTools
    except ImportError:
        return

    def _stub_web_search(self, query: str, max_results: int = 5) -> str:
        return (
            "Web search returned no results in the eval environment. "
            "Proceed using the project's own files and conventions."
        )

    def _stub_search_news(self, query: str, max_results: int = 5) -> str:
        return "Web news search returned no results in the eval environment."

    DuckDuckGoTools.web_search = _stub_web_search  # type: ignore[assignment]
    DuckDuckGoTools.search_news = _stub_search_news  # type: ignore[assignment]


_install_websearch_stub()


async def _auto_approve_loop(coord) -> None:
    """Drain the sub-agent HITL coordinator and confirm every request.

    The TUI's BE multiplexer drains this in production — see
    ``backend/server.py:_stream_with_subagent_hitl``. The smoke runner
    has no UI, so we replicate just the "auto-confirm" part: each time
    a specialist pauses for permission, immediately resolve as
    ``confirm`` so the spawn unblocks and the run continues.
    """
    while True:
        try:
            await coord.new_arrival.wait()
            entries = coord.list_new_pending()
            for req_id, _entry in entries:
                coord.resolve(req_id, "confirm")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # Don't let an isolated failure kill the drain — it would
            # silently re-introduce the hang we're trying to avoid.
            print(f"  ! auto-approve drain error (continuing): {exc}")


def _categorize(case_name: str, suite_agent: str) -> str:
    """Map case name → category label for aggregate reporting."""
    n = case_name
    if suite_agent == "editor":
        if any(k in n for k in ("grep", "glob", "cat", "_uses_", "edit_not_write", "create_new_file", "read_before_edit")):
            return "A. tool-selection"
        if any(k in n for k in ("rm_rf", "secrets", "sql", "xss", "auth_check", "security_feature", "write_for_single", "edit_without_read")):
            return "B. anti-patterns"
        if any(k in n for k in ("minimal_diff", "unrelated_tests", "retry_only", "no_docstrings", "impossible", "three_lines")):
            return "C. minimal-diff"
        if any(k in n for k in ("tabs", "quotes", "camelcase", "function_keyword")):
            return "D. style"
        if any(k in n for k in ("runs_tests", "tests_fail", "linter", "ember_md_conv")):
            return "E. verification"
        if any(k in n for k in ("ambiguous", "conflicting_ember", "out_of_scope", "preexisting_security", "spawns_explorer", "spawn_for_simple")):
            return "F. judgement"
        return "G. specific"
    if suite_agent == "main":
        if n.startswith("direct_"):
            return "A. Direct (no delegation)"
        if n.startswith("single_"):
            return "B. Single-specialist"
        if n.startswith("broadcast_"):
            return "C. Broadcast"
        if n.startswith("tasks_"):
            return "D. Tasks mode"
        if n.startswith("anti_"):
            return "E. Anti-patterns"
        if n.startswith("routing_"):
            return "F. Routing"
    return "uncategorized"


def _build_live_model():
    """Construct the OpenAI-compatible model wired to EMBER_TEST_LLM_* env vars."""
    from agno.models.openai.like import OpenAILike

    return OpenAILike(
        id=os.getenv("EMBER_TEST_LLM_MODEL") or "gpt-4o-mini",
        api_key=os.environ["EMBER_TEST_LLM_API_KEY"],
        base_url=os.getenv("EMBER_TEST_LLM_BASE_URL") or "https://api.openai.com/v1",
    )


def _build_editor_agent(model, project_dir: Path):
    """Build the editor specialist directly from agents/editor.md.

    Avoids spinning up the full Session — we just need an Agent object with
    the editor's prompt + the file-ops toolkit.
    """
    from agno.agent import Agent

    from ember_code.core.config.tool_permissions import ToolPermissions
    from ember_code.core.tools.registry import ToolRegistry

    md = Path("agents/editor.md").read_text()
    fm_match = re.match(r"^---\n.*?\n---\n(.*)", md, re.DOTALL)
    system_prompt = fm_match.group(1) if fm_match else md

    registry = ToolRegistry(
        base_dir=str(project_dir),
        permissions=ToolPermissions(project_dir=project_dir),
    )
    # Option B toolkit: shell-native primary + structured tools for write/edit only.
    # Drop Read, Grep, Glob — the model uses run_shell_command for those instead.
    tools = registry.resolve(["Write", "Edit", "Bash"])

    return Agent(
        name="editor",
        model=model,
        instructions=system_prompt,
        tools=tools,
        markdown=True,
    )


def _build_mcp_test_agent(model, project_dir: Path):
    """Build a small agent exposing fake MCP-shaped tools alongside
    standard tools, for routing-decision evals.

    See ``evals/_mcp_stubs.py`` for the fake tools. We give the agent a
    minimal, mode-neutral system prompt so the test measures *the
    model's own tool-routing instincts*, not specialized prompt
    coaching.
    """
    import sys

    from agno.agent import Agent

    from ember_code.core.config.tool_permissions import ToolPermissions
    from ember_code.core.tools.registry import ToolRegistry

    sys.path.insert(0, str(Path("evals").resolve()))
    from _mcp_stubs import MCPStubTools  # type: ignore[import-not-found]

    registry = ToolRegistry(
        base_dir=str(project_dir),
        permissions=ToolPermissions(project_dir=project_dir),
    )
    tools = registry.resolve(["Write", "Edit", "Bash", "Read"])
    tools.append(MCPStubTools())

    instructions = (
        "You are a coding assistant with both standard file/shell tools "
        "and several MCP-surfaced integration tools "
        "(``mcp__linear__create_issue``, ``mcp__notion__create_page``, "
        "``mcp__slack__post_message``, ``mcp__github__create_issue``).\n\n"
        "Pick the tool that *fits the task*. Use MCP tools when the user "
        "asks for an action against the corresponding service (Linear, "
        "Notion, Slack, GitHub). Use shell / edit_file / save_file for "
        "code work. Don't use MCP tools for code edits, and don't shell "
        "out to ``gh`` / ``curl`` when an MCP tool exists for the same "
        "operation."
    )
    return Agent(
        name="mcp-test",
        model=model,
        instructions=instructions,
        tools=tools,
        markdown=True,
    )


def _build_main_agent(model, project_dir: Path):
    """Build the main orchestrator agent.

    Heavier — needs the full Session because OrchestrateTools depends on a
    populated AgentPool to dispatch to specialists. We point Session at a
    temp project_dir so we don't touch the real .ember/ workspace.
    """
    from ember_code.core.config.settings import load_settings
    from ember_code.core.session.core import Session

    # Override the default model to use our live test model
    os.environ["EMBER_TEST_LLM_API_KEY_OVERRIDE"] = os.environ["EMBER_TEST_LLM_API_KEY"]

    settings = load_settings(project_dir=project_dir)
    # Patch settings to redirect the default model at our test endpoint
    test_model_id = os.getenv("EMBER_TEST_LLM_MODEL") or "gpt-4o-mini"
    test_base_url = os.getenv("EMBER_TEST_LLM_BASE_URL") or "https://api.openai.com/v1"
    test_api_key = os.environ["EMBER_TEST_LLM_API_KEY"]

    settings.models.registry["MiniMax-M2.7"] = {
        "provider": "openai_like",
        "model_id": test_model_id,
        "url": test_base_url,
        "api_key": test_api_key,
        "context_window": 128_000,
        "vision": False,
    }
    settings.models.default = "MiniMax-M2.7"

    session = Session(settings=settings, project_dir=project_dir)
    return session.main_team


async def _run_case(
    case,
    agent,
    judge_model,
    case_timeout: float = 60.0,
    case_retries: int = 2,
    work_dir: Path | None = None,
):
    """Run a single case with a fresh session_id so history doesn't leak.

    Hard-capped at ``case_timeout`` seconds per attempt. The harness
    retries up to ``case_retries`` times on **environmental** failures:

    * Transient model errors (429s, connection drops, provider hiccups)
    * ``case_timeout`` — initially we treated these as terminal, but
      they're often the *symptom* of contention on shared agent state
      (concurrent spawns of the same specialist racing on
      ``run_id``/``session_id``). Retrying after the parallel cases
      finish gives a clean run a chance, and the worst case is just
      ``attempts × case_timeout`` of wasted wall-clock.

    Cancellation is propagated unchanged. After ``1 + case_retries``
    attempts we return a CaseResult with ``error`` set rather than
    raise, so the suite always finishes.
    """
    import uuid as _uuid
    from ember_code.core.evals.runner import CaseResult, run_eval_case

    last_error: str | None = None
    attempts = 1 + max(0, case_retries)
    # Per-case override wins. Tasks-mode / multi-specialist cases need
    # more wall-clock than the suite default; pinning a longer timeout
    # in the YAML beats globally inflating the default.
    effective_timeout = case.case_timeout or case_timeout

    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(
                run_eval_case(
                    case,
                    agent,
                    judge_model,
                    session_id=f"eval-{_uuid.uuid4().hex[:8]}",
                    work_dir=work_dir,
                ),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            last_error = f"case_timeout: exceeded {effective_timeout:.0f}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = f"model_error: {exc}"
        if attempt < attempts:
            # Exponential backoff: 2s, 4s, 8s. Bounded so we don't
            # waste the whole suite recovering from a long outage.
            backoff = min(2**attempt, 30)
            await asyncio.sleep(backoff)

    result = CaseResult(case=case)
    result.error = f"{last_error} (after {attempts} attempts)"
    result.elapsed = effective_timeout if last_error and "case_timeout" in last_error else 0.0
    return result


def _dump_case_result(results_dir: Path, suite_agent: str, case_result) -> None:
    """Write per-case JSON to results_dir/<suite>/<case_name>.json."""
    suite_dir = results_dir / suite_agent
    suite_dir.mkdir(parents=True, exist_ok=True)
    payload = case_result.model_dump(mode="json")
    # Slim the tool_trace args/result previews for readability
    out_path = suite_dir / f"{case_result.case.name}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str))


async def _run_suite(
    suite_path: Path,
    agent_factory,
    judge_model,
    n_cases: int | None,
    fixtures_root: Path | None,
    concurrency: int,
    results_dir: Path | None = None,
    case_timeout: float = 60.0,
    case_retries: int = 2,
):
    """Run all cases concurrently, each in its own isolated work_dir.

    ``agent_factory`` is a callable that takes a ``base_dir`` Path and
    returns a fresh agent. We can't share one agent across concurrent
    runs — Agno's Agent object holds per-run state (run_id,
    run_response, history) that races under ``asyncio.gather``.

    Per-case isolation also keeps file mutations from contaminating
    other cases when they run in parallel.
    """
    import shutil

    from ember_code.core.evals.loader import load_eval_file
    from ember_code.core.evals.runner import _setup_fixtures

    suite = load_eval_file(suite_path)
    if not suite:
        print(f"  ! could not load {suite_path}")
        return None

    cases = suite.cases if n_cases is None else suite.cases[:n_cases]
    print(f"\n=== {suite.agent} — running {len(cases)}/{len(suite.cases)} cases  "
          f"(concurrency={concurrency}) ===")

    sem = asyncio.Semaphore(concurrency)

    async def _one(idx: int, case):
        async with sem:
            case_work = Path(tempfile.mkdtemp(prefix=f"ember-eval-c{idx:02d}-"))
            if suite.fixtures and fixtures_root and fixtures_root.is_dir():
                _setup_fixtures(suite.fixtures, fixtures_root, work_dir=case_work)
            try:
                agent = agent_factory(case_work)
                try:
                    r = await _run_case(
                        case,
                        agent,
                        judge_model,
                        case_timeout=case_timeout,
                        case_retries=case_retries,
                        work_dir=case_work,
                    )
                    return idx, case, r, None
                except Exception as exc:
                    return idx, case, None, exc
            finally:
                shutil.rmtree(case_work, ignore_errors=True)

    tasks = [asyncio.create_task(_one(i, c)) for i, c in enumerate(cases, 1)]
    completed = 0
    pending_print: dict[int, tuple] = {}
    next_to_print = 1

    # Print results in submission order even though they complete out of
    # order — easier to read, easier to compare across runs.
    for fut in asyncio.as_completed(tasks):
        idx, case, r, exc = await fut
        completed += 1
        pending_print[idx] = (case, r, exc)
        while next_to_print in pending_print:
            c, rr, ex = pending_print.pop(next_to_print)
            if ex is not None:
                print(f"  [{next_to_print:>2}] ERROR {c.name}: {ex}")
            else:
                # Distinguish a real eval FAIL from an environmental
                # error (rate-limit / connection / case_timeout) so the
                # eval signal isn't muddied by infrastructure noise.
                err = (rr.error or "").lower()
                is_env_error = any(
                    tag in err
                    for tag in ("model_error", "case_timeout", "agent.arun failed")
                )
                if rr.passed:
                    status = "PASS"
                elif is_env_error:
                    status = "ENV"
                else:
                    status = "FAIL"
                print(f"  [{next_to_print:>2}] {status:5} {c.name}  ({rr.elapsed:.1f}s)")
                if not rr.passed:
                    if rr.reliability_detail and rr.reliability_passed is False:
                        print(f"          rel: {rr.reliability_detail}")
                    if rr.unexpected_detail and rr.unexpected_passed is False:
                        print(f"          unexp: {rr.unexpected_detail}")
                    if rr.tool_arg_detail and rr.tool_arg_passed is False:
                        print(f"          arg: {rr.tool_arg_detail}")
                    if rr.accuracy_score is not None and rr.accuracy_passed is False:
                        print(f"          acc: {rr.accuracy_score:.1f}/{rr.case.accuracy_threshold}")
                    if rr.error:
                        print(f"          err: {rr.error}")
            next_to_print += 1

    results = []
    for t in tasks:
        idx, case, r, exc = t.result()
        if r is not None:
            results.append((case, r))
            if results_dir is not None:
                try:
                    _dump_case_result(results_dir, suite.agent, r)
                except Exception as dump_exc:
                    print(f"  ! failed to dump {case.name}: {dump_exc}")
    return suite, results


def _dump_summary(results_dir: Path, suite, results) -> None:
    """Write a per-suite summary JSON: counts, pass-rate by category,
    and one-line per-case status. Lets us diff runs quickly."""
    by_cat: dict[str, list[bool]] = defaultdict(list)
    cases_summary = []
    for case, r in results:
        cat = _categorize(case.name, suite.agent)
        by_cat[cat].append(r.passed)
        cases_summary.append({
            "name": case.name,
            "category": cat,
            "passed": r.passed,
            "elapsed": round(r.elapsed, 2),
            "reliability_passed": r.reliability_passed,
            "reliability_detail": r.reliability_detail,
            "unexpected_passed": r.unexpected_passed,
            "unexpected_detail": r.unexpected_detail,
            "accuracy_score": r.accuracy_score,
            "accuracy_passed": r.accuracy_passed,
            "tools_called": [t["name"] for t in r.tool_trace],
            "error": r.error,
        })
    summary = {
        "agent": suite.agent,
        "total": len(results),
        "passed": sum(1 for _, r in results if r.passed),
        "by_category": {
            cat: {"passed": sum(v), "total": len(v)} for cat, v in by_cat.items()
        },
        "cases": cases_summary,
    }
    out = results_dir / f"{suite.agent}_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=str))


def _summarize(suite, results):
    """Print pass-rate per category for one suite."""
    by_cat: dict[str, list[bool]] = defaultdict(list)
    for case, r in results:
        by_cat[_categorize(case.name, suite.agent)].append(r.passed)

    n_pass = sum(1 for _, r in results if r.passed)
    n_total = len(results)
    pct = (n_pass / n_total * 100) if n_total else 0
    print(f"\n  Overall: {n_pass}/{n_total} ({pct:.0f}%)")
    for cat in sorted(by_cat):
        vals = by_cat[cat]
        p = sum(vals)
        print(f"    {cat:38s}  {p}/{len(vals)}  ({p/len(vals)*100:.0f}%)")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=5,
                        help="cases per suite (default 5; use 0 or negative for ALL)")
    # The set of suites you can run. Each name maps to evals/<name>.yaml
    # (with `main` mapping to evals/main_agent.yaml). `all` runs every
    # *.yaml under evals/. You can also pass a comma-list of names.
    SUITE_CHOICES = [
        "all",
        "main",
        "editor",
        "explorer",
        "architect",
        "conversational",
        "debugger",
        "diagnostician",
        "docs",
        "git",
        "planner",
        "qa",
        "reviewer",
        "security",
        "simplifier",
    ]
    parser.add_argument(
        "--suite",
        default="all",
        help=f"one of {SUITE_CHOICES}, or a comma-list of names",
    )
    parser.add_argument("--concurrency", type=int, default=5,
                        help="parallel cases per suite (default 5)")
    parser.add_argument(
        "--case-retries",
        type=int,
        default=2,
        help=(
            "harness-level retries on transient model errors (e.g. 429, "
            "connection reset). Does NOT retry on case_timeout — those "
            "indicate a stuck case, not a flake. Total attempts = 1 + "
            "case_retries (default: 1+2=3)."
        ),
    )
    parser.add_argument(
        "--spawn-timeout",
        type=int,
        default=None,
        help=(
            "override settings.orchestration.sub_team_timeout (seconds). "
            "Lets a hung specialist abort early so a slow main_agent run "
            "doesn't wedge the whole eval. Default keeps the project setting."
        ),
    )
    parser.add_argument(
        "--case-timeout",
        type=int,
        default=60,
        help=(
            "hard cap (seconds) on a single eval case. A case that doesn't "
            "finish in this window is marked FAIL with reason=case_timeout. "
            "60s is enough for any healthy multi-agent flow; longer means a "
            "retry storm or a stuck stream — neither is what we want to "
            "report as a partial PASS."
        ),
    )
    args = parser.parse_args()

    if not os.getenv("EMBER_TEST_LLM_API_KEY"):
        # Try to load from .env
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        if not os.getenv("EMBER_TEST_LLM_API_KEY"):
            print("Set EMBER_TEST_LLM_API_KEY (in .env or env).")
            sys.exit(1)

    n = args.cases if args.cases > 0 else None
    concurrency = max(1, args.concurrency)

    judge_model = _build_live_model()  # judge: separate instance for cleanliness
    fixtures_root = Path("evals/fixtures").resolve() if Path("evals/fixtures").is_dir() else None

    # Timestamped results directory — one folder per smoke run.
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    results_dir = Path("evals/results") / stamp
    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"  results dir: {results_dir}")

    # Resolve which suites to run.
    requested: list[str]
    if args.suite == "all":
        requested = sorted(p.stem for p in Path("evals").glob("*.yaml"))
        # Translate filename → suite agent name (main_agent.yaml drives "main")
        requested = [r.replace("main_agent", "main") for r in requested]
    else:
        requested = [s.strip() for s in args.suite.split(",") if s.strip()]

    # Per-case Session build: every case gets a fresh Session rooted at
    # its own work_dir. The earlier "build once, reuse" pattern shared
    # one project_dir across all cases — that meant case N saw artifacts
    # from cases 1..N-1, which broke isolation for any test that did
    # ``ls`` / ``find`` to discover the project shape. Slower (~5-10s
    # per case for the Session build), but every test now starts from
    # the fixture's clean state.
    spawn_timeout_override = args.spawn_timeout

    def _build_session_for(project_dir: Path):
        """Build a fresh Session rooted at ``project_dir``.

        Writes a permissive ``.ember/settings.local.json`` under
        ``project_dir`` so the agent can use Bash/Edit/Write without
        HITL confirmation (evals are headless), wires the test model,
        disables Agno's exception retries, and starts an auto-approve
        loop on the sub-agent HITL coordinator. Returns the live
        Session — caller decides whether to use ``main_team`` or
        ``pool.get(name)``.
        """
        from ember_code.core.config.settings import load_settings
        from ember_code.core.session.core import Session

        import json as _json

        ember_dir = project_dir / ".ember"
        ember_dir.mkdir(parents=True, exist_ok=True)
        (ember_dir / "settings.local.json").write_text(
            _json.dumps(
                {
                    "permissions": {
                        "allow": [
                            "Glob",
                            "Grep",
                            "LS",
                            "Read",
                            "WebSearch",
                            "WebFetch",
                            "Bash",
                            "BashOutput",
                            "Write",
                            "Edit",
                            "Python",
                            "Schedule",
                        ],
                        "ask": [],
                    }
                }
            )
        )

        settings = load_settings(project_dir=project_dir)
        test_model_id = os.getenv("EMBER_TEST_LLM_MODEL") or "gpt-4o-mini"
        test_base_url = os.getenv("EMBER_TEST_LLM_BASE_URL") or "https://api.openai.com/v1"
        test_api_key = os.environ["EMBER_TEST_LLM_API_KEY"]
        settings.models.registry["MiniMax-M2.7"] = {
            "provider": "openai_like",
            "model_id": test_model_id,
            "url": test_base_url,
            "api_key": test_api_key,
            "context_window": 128_000,
            "vision": False,
        }
        settings.models.default = "MiniMax-M2.7"
        if spawn_timeout_override is not None:
            settings.orchestration.sub_team_timeout = spawn_timeout_override
        # Disable Agno's retry-on-exception so we get deterministic
        # single-shot results (retry amplification turned a 60s timeout
        # into ~540s case wedges).
        settings.models.retries = 0
        sess = Session(settings=settings, project_dir=project_dir)
        # Auto-approve every sub-agent HITL request. Evals are headless
        # — without this drain, a specialist hitting any
        # ``requires_confirmation`` tool blocks forever on
        # ``coord.wait_resolved``.
        asyncio.create_task(_auto_approve_loop(sess.sub_agent_hitl))
        return sess

    def _factory_for(suite_name: str):
        """Return a (yaml_path, factory) pair for the given suite name.

        The suite *name* is just the YAML filename stem; the actual
        target *agent* is whatever the YAML's ``agent:`` field says.
        That lets cross-cutting suites (e.g. ``schedule.yaml``,
        ``agent: main``) live alongside per-specialist suites without
        renaming gymnastics.
        """
        # Suite filename: ``main`` aliases to the legacy file name,
        # everything else is just ``<name>.yaml``.
        if suite_name == "main":
            yaml_path = Path("evals/main_agent.yaml")
        else:
            yaml_path = Path("evals") / f"{suite_name}.yaml"
        if not yaml_path.is_file():
            print(f"  ! suite '{suite_name}' not found at {yaml_path} — skipping")
            return None, None

        # Read the YAML's ``agent:`` to know which specialist (or the
        # main team) to instantiate. Avoids hard-coding suite→agent
        # mappings as the suite list grows.
        try:
            import yaml as _yaml

            with open(yaml_path) as _f:
                _doc = _yaml.safe_load(_f) or {}
            target_agent = str(_doc.get("agent", suite_name))
        except Exception as exc:
            print(f"  ! could not read agent from {yaml_path} ({exc}) — skipping")
            return None, None

        # Editor has its own bypass factory (no Session, faster).
        if target_agent == "editor":

            def editor_factory(base_dir: Path):
                return _build_editor_agent(_build_live_model(), base_dir)

            return yaml_path, editor_factory

        # MCP suite: bypass-style factory with stub MCP tools attached.
        # Avoids depending on real out-of-process MCP servers.
        if target_agent == "mcp-test":

            def mcp_factory(base_dir: Path):
                return _build_mcp_test_agent(_build_live_model(), base_dir)

            return yaml_path, mcp_factory

        if target_agent == "main":

            def main_factory(base_dir: Path):
                # Fresh Session per case → main_team's tools resolve
                # paths against the per-case work_dir, not a shared
                # project_dir polluted by prior cases.
                sess = _build_session_for(base_dir)
                return sess.main_team

            return yaml_path, main_factory

        # Specialist agent — build a fresh Session per case and pull
        # the named specialist out of its pool. Same isolation reason
        # as the main agent: avoid cross-case state leakage.
        try:
            # Probe once with a throwaway session_dir to confirm the
            # specialist exists in the pool before the suite runs;
            # gives a clean error message instead of a per-case crash.
            probe_dir = Path(tempfile.mkdtemp(prefix="ember-eval-probe-"))
            probe_sess = _build_session_for(probe_dir)
            probe_sess.pool.get(target_agent)
        except (KeyError, ValueError) as exc:
            print(f"  ! agent '{target_agent}' not in pool ({exc}) — skipping")
            return None, None

        def specialist_factory(base_dir: Path):
            sess = _build_session_for(base_dir)
            return sess.pool.get(target_agent)

        return yaml_path, specialist_factory

    for agent_name in requested:
        yaml_path, factory = _factory_for(agent_name)
        if factory is None:
            continue
        outcome = await _run_suite(
            yaml_path,
            factory,
            judge_model,
            n,
            fixtures_root,
            concurrency,
            results_dir=results_dir,
            case_timeout=args.case_timeout,
            case_retries=args.case_retries,
        )
        if outcome:
            _summarize(*outcome)
            _dump_summary(results_dir, *outcome)


if __name__ == "__main__":
    asyncio.run(main())
