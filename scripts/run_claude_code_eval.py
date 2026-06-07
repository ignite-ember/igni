"""Run an eval suite against Claude Code (the CLI), one case at a time.

Each case's ``input`` is sent as a one-shot prompt to ``claude -p`` with
``cwd=<target_project_dir>``. Each invocation is a fresh Claude Code session
— no memory carry-over, no access to the eval YAML or expected outputs
(those live in the ember-code repo, outside the working dir). Responses
are captured and graded by the same Agno ``AccuracyEval`` ember-code's
runner uses, with a configurable judge model (default: the
``EMBER_TEST_LLM_*`` triple, which currently points at MiniMax-M2.7 — keep
the judge separate from the agent's model to avoid self-grading bias).

Output JSON shape mirrors ``run_codeindex_eval.py``'s ``--out`` so the
result can sit next to ``v10-with.json`` for side-by-side comparison.

Usage:

    python scripts/run_claude_code_eval.py \\
        --suite ember_server_v6 \\
        --target-project-dir /path/to/ember-server \\
        --out /tmp/eval-comparison/claude-code-with.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
for n in ("httpx", "httpcore", "urllib3", "agno", "chromadb", "asyncio"):
    logging.getLogger(n).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _run_claude_code(prompt: str, cwd: Path, timeout: int) -> tuple[str, float, dict]:
    """Run a single ``claude -p`` invocation and return (response_text, elapsed, meta).

    Uses ``--output-format json`` so we get a structured response with
    cost / token / duration fields alongside the answer text. Bypasses
    permissions because eval runs are non-interactive — claude has to
    be able to read files in ``cwd`` without prompting.
    """
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
    ]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        return "", elapsed, {"error": "timeout", "stderr": ""}

    elapsed = time.monotonic() - t0
    if proc.returncode != 0:
        return (
            "",
            elapsed,
            {
                "error": f"claude exit {proc.returncode}",
                "stderr": proc.stderr[-2000:] if proc.stderr else "",
            },
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return (
            "",
            elapsed,
            {"error": f"json decode: {exc}", "raw": proc.stdout[:1000]},
        )

    response_text = payload.get("result") or payload.get("response") or ""
    meta = {
        "session_id": payload.get("session_id"),
        "num_turns": payload.get("num_turns"),
        "duration_ms": payload.get("duration_ms"),
        "duration_api_ms": payload.get("duration_api_ms"),
        "total_cost_usd": payload.get("total_cost_usd"),
        "model": payload.get("model")
        or payload.get("modelUsage", {}).get("model")
        if isinstance(payload.get("modelUsage"), dict)
        else None,
    }
    return response_text, elapsed, meta


async def _grade_response(
    case: Any, response_text: str, judge_model: Any
) -> tuple[bool | None, float | None, str]:
    """Grade ``response_text`` against ``case.expected_output`` using
    Agno's ``AccuracyEval`` — same machinery ember-code's eval runner
    uses for ``accuracy_passed`` / ``accuracy_score`` / ``accuracy_reason``.

    Skips grading (returns Nones) if the case has no expected output or
    no judge model was configured.
    """
    if not case.expected_output or judge_model is None:
        return None, None, ""
    try:
        from agno.agent import Agent
        from agno.eval.accuracy import AccuracyEval
    except ImportError as exc:
        logger.warning("agno.eval.accuracy not available: %s", exc)
        return None, None, ""

    placeholder = Agent(model=judge_model)
    acc = AccuracyEval(
        agent=placeholder,
        input=case.input,
        expected_output=case.expected_output,
        model=judge_model,
        additional_guidelines=case.judge_guidelines,
        num_iterations=case.num_iterations,
        print_summary=False,
        print_results=False,
        telemetry=False,
    )
    result = await acc.arun_with_output(
        output=response_text,
        print_summary=False,
        print_results=False,
    )
    if result is None:
        return False, None, "accuracy eval returned None"
    score = result.avg_score
    threshold = case.accuracy_threshold
    passed = score >= threshold
    reasons: list[str] = []
    for r in getattr(result, "results", None) or []:
        reason = getattr(r, "reason", None)
        if reason:
            reasons.append(reason)
    return passed, score, "\n---\n".join(reasons)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        default="ember_server_v6",
        help="Eval suite YAML name (without .yaml). Default: ember_server_v6.",
    )
    parser.add_argument(
        "--target-project-dir",
        type=Path,
        required=True,
        help="Project root passed as cwd to ``claude -p``. Each case runs here.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Path to write the JSON result.",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip the AccuracyEval grading step. Useful if you only want to "
        "capture responses and grade them out-of-band.",
    )
    args = parser.parse_args()

    ember_code_dir = Path(__file__).resolve().parent.parent
    yaml_path = ember_code_dir / "evals" / f"{args.suite}.yaml"
    target_dir = args.target_project_dir.resolve()
    if not target_dir.exists():
        print(f"target dir does not exist: {target_dir}", file=sys.stderr)
        return 1
    if not yaml_path.exists():
        print(f"suite yaml not found: {yaml_path}", file=sys.stderr)
        return 1

    if str(ember_code_dir) not in sys.path:
        sys.path.insert(0, str(ember_code_dir))

    from ember_code.core.config.settings import Settings
    from ember_code.core.evals.loader import load_eval_file
    from ember_code.core.session.core import ModelRegistry

    suite = load_eval_file(yaml_path)
    if suite is None:
        print(f"failed to load suite from {yaml_path}", file=sys.stderr)
        return 1

    # Auto-source .env so the judge can connect — same pattern as run_codeindex_eval.
    if not os.environ.get("EMBER_TEST_LLM_API_KEY"):
        env_path = ember_code_dir / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)

    # Build a judge model from the same EMBER_TEST_LLM_* triple the
    # ember-code eval runner uses. Keeps the judge model independent of
    # the agent (Claude Code) — avoids "Claude grades Claude" bias.
    judge_model = None
    if not args.no_judge:
        api_key = os.environ.get("EMBER_TEST_LLM_API_KEY")
        if not api_key:
            print(
                "WARNING: EMBER_TEST_LLM_API_KEY not set — running without judge.",
                file=sys.stderr,
            )
        else:
            settings = Settings()
            base_url = os.environ.get(
                "EMBER_TEST_LLM_BASE_URL", "https://api.minimax.io/v1"
            )
            model_id = os.environ.get("EMBER_TEST_LLM_MODEL", "MiniMax-M2.7")
            settings.models.registry["judge-model"] = {
                "provider": "openai_like",
                "model_id": model_id,
                "url": base_url,
                "api_key": api_key,
                "context_window": 200_000,
                "vision": False,
                "timeout": 90,
            }
            settings.models.default = "judge-model"
            judge_model = ModelRegistry(settings).get_model()
            print(
                f"Judge model: {model_id} @ {base_url}",
                file=sys.stderr,
            )

    print(
        f"Running {len(suite.cases)} cases via claude -p against {target_dir}...",
        file=sys.stderr,
    )

    case_results: list[dict] = []
    suite_t0 = time.monotonic()
    for i, case in enumerate(suite.cases, 1):
        case_timeout = max(120, int(getattr(case, "case_timeout", 360)) + 60)
        print(f"  [case {i}/{len(suite.cases)}] {case.name} starting...", file=sys.stderr)
        sys.stderr.flush()
        response_text, elapsed, meta = _run_claude_code(
            case.input, target_dir, case_timeout
        )
        verdict = "ERROR" if meta.get("error") else "ran"
        print(
            f"  [case {i}/{len(suite.cases)}] {case.name} → {verdict} in {elapsed:.1f}s "
            f"(turns={meta.get('num_turns')}, cost=${meta.get('total_cost_usd')})",
            file=sys.stderr,
        )

        passed_judge: bool | None = None
        score: float | None = None
        reason = ""
        if response_text and judge_model is not None:
            try:
                passed_judge, score, reason = await _grade_response(
                    case, response_text, judge_model
                )
            except Exception as exc:
                logger.exception("judge failed for %s", case.name)
                reason = f"judge exception: {exc}"

        # ``passed`` mirrors ember-code's CaseResult — true when the
        # judge passed (or no judge was set and the run completed).
        if meta.get("error"):
            passed_overall = False
        elif passed_judge is None:
            passed_overall = bool(response_text)
        else:
            passed_overall = passed_judge

        case_results.append(
            {
                "case": case.model_dump(mode="json"),
                "passed": passed_overall,
                "response_text": response_text,
                "elapsed": elapsed,
                "claude_meta": meta,
                "accuracy_passed": passed_judge,
                "accuracy_score": score,
                "accuracy_reason": reason,
                "error": meta.get("error"),
            }
        )

    suite_elapsed = time.monotonic() - suite_t0
    passed_count = sum(1 for r in case_results if r["passed"])
    total = len(case_results)

    payload = {
        "mode": "claude-code (no CodeIndex)",
        "target_project_dir": str(target_dir),
        "cases": case_results,
        "totals": {
            "passed": passed_count,
            "failed": total - passed_count,
            "total": total,
            "elapsed": suite_elapsed,
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str))
    print(
        f"\nDone. {passed_count}/{total} passed in {suite_elapsed:.0f}s. "
        f"Detail written to {args.out}",
        file=sys.stderr,
    )
    return 0 if passed_count == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
