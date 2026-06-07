"""Re-judge codewrite eval results on duplication-risk axes.

The standard accuracy judge (Agno AccuracyEval) gives one score per
case. The codewrite cases need finer per-axis scoring to surface
the duplication story:

  A. LOCATED EXISTING INFRA — did the agent inspect what's already
     there?
  B. REUSED CONVENTIONS — does the proposed code use existing
     classes/helpers/prefixes?
  C. NOT DUPLICATED — did the agent avoid creating parallel infra
     when an existing version already covers the use case?

Plus a free-text ``hallucinations`` field for any invented file
paths, class names, or APIs the response references that don't
exist in the target codebase.

Reads two run JSONs (WITH / WITHOUT codeindex), prompts the
eval-model (same MiniMax-M2.7 used by the original eval) with each
response + its judge_guidelines, parses structured JSON back,
prints a per-case A/B/C comparison table and dumps a summary JSON.

Usage::

    uv run python scripts/codewrite_judge.py \\
        --with /tmp/eval-comparison/codewrite-with.json \\
        --without /tmp/eval-comparison/codewrite-without.json \\
        --out /tmp/eval-comparison/codewrite-judged.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field


class AxisScore(BaseModel):
    score: int = Field(..., ge=0, le=3, description="0=missed, 1=partial, 2=mostly, 3=full")
    reason: str


class CaseJudgement(BaseModel):
    case_name: str
    mode: str
    located: AxisScore
    reused: AxisScore
    not_duplicated: AxisScore
    hallucinations: list[str] = Field(default_factory=list)
    overall: str  # PASS / FAIL — agent's own pass/fail roll-up


JUDGE_PROMPT_TEMPLATE = """You are evaluating an AI agent's response to a code-write task.
Your job is to score it on three duplication-risk axes A/B/C, list any
hallucinations (invented file paths, class names, APIs that don't
exist in the target codebase), and give an overall PASS/FAIL.

Use ONLY the rubric below — do not invent new criteria.

## Task given to the agent

{task_input}

## Expected behavior

{expected_output}

## Rubric (judge-guidelines from the eval suite)

{judge_guidelines}

## The agent's response

{response_text}

## Your output

Respond with a single JSON object on one line, matching this schema
EXACTLY (no markdown fences, no commentary, no extra fields):

{{"located": {{"score": 0-3, "reason": "..."}},
  "reused": {{"score": 0-3, "reason": "..."}},
  "not_duplicated": {{"score": 0-3, "reason": "..."}},
  "hallucinations": ["..."],
  "overall": "PASS" | "FAIL"}}

Score scale: 0=missed entirely, 1=partial/weak, 2=mostly, 3=fully.
"hallucinations" must list specific tokens (class names, file
paths, API names) the agent referenced that aren't in the
ember-server codebase, or be ``[]`` if none. Don't speculate —
only list things you can name.
"""


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first {...} block out of LLM output. Models sometimes
    wrap the JSON in markdown fences, prefix it with prose, emit a
    ``<think>...</think>`` reasoning block, or use single-quoted keys
    despite instructions; this normalizes all of those."""
    text = text.strip()
    # MiniMax-M2.7 emits a <think>...</think> reasoning block before
    # the answer. Strip it (DOTALL so newlines inside don't escape).
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Strip markdown fences if present (with or without language tag).
    text = re.sub(r"```[a-zA-Z0-9]*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    # Grab the outermost {...} span if there's surrounding prose.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last-ditch: convert single quotes to double around keys/values.
        # Models occasionally emit Python-dict-style output.
        fixed = re.sub(r"(?<![a-zA-Z0-9])'([^']*?)'", r'"\1"', text)
        return json.loads(fixed)


async def judge_one(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model_id: str,
    case_name: str,
    mode: str,
    task_input: str,
    expected_output: str,
    judge_guidelines: str,
    response_text: str,
) -> CaseJudgement:
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        task_input=task_input.strip(),
        expected_output=expected_output.strip(),
        judge_guidelines=judge_guidelines.strip(),
        response_text=response_text.strip(),
    )
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "You are a strict code-review judge. Respond with ONLY a JSON object."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 1500,
    }
    resp = await client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["choices"][0]["message"]["content"]
    try:
        parsed = extract_json(raw)
        return CaseJudgement(case_name=case_name, mode=mode, **parsed)
    except Exception as exc:
        # Retry once with a sharper system prompt. The model sometimes
        # ignores the JSON-only instruction and emits a prose preamble
        # ("Let me analyze..."); a one-shot retry that quotes the
        # required schema fixes most of those cases.
        print(
            f"  parse failure for {mode} {case_name}: {exc} — retrying with strict prompt",
            file=sys.stderr,
        )
        retry_payload = {
            "model": model_id,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a strict code-review judge. Your previous answer was "
                        "not valid JSON. Output ONLY a JSON object — no prose, no "
                        "markdown fences, no <think> blocks, no preamble. The "
                        "schema is exactly: "
                        '{"located":{"score":0-3,"reason":"..."},'
                        '"reused":{"score":0-3,"reason":"..."},'
                        '"not_duplicated":{"score":0-3,"reason":"..."},'
                        '"hallucinations":["..."],"overall":"PASS"|"FAIL"}'
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 800,
        }
        try:
            resp2 = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=retry_payload,
                timeout=120.0,
            )
            resp2.raise_for_status()
            raw2 = resp2.json()["choices"][0]["message"]["content"]
            parsed = extract_json(raw2)
            return CaseJudgement(case_name=case_name, mode=mode, **parsed)
        except Exception as exc2:
            print(
                f"  WARN: retry also failed for {mode} {case_name}: {exc2}\n"
                f"  raw output (first 400 chars): {raw[:400]!r}",
                file=sys.stderr,
            )
            return CaseJudgement(
                case_name=case_name,
                mode=mode,
                located=AxisScore(score=0, reason=f"judge parse failure: {exc2}"),
                reused=AxisScore(score=0, reason="judge parse failure"),
                not_duplicated=AxisScore(score=0, reason="judge parse failure"),
                hallucinations=[],
                overall="FAIL",
            )


def load_run(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(mode_label, cases)`` from a run JSON dump."""
    payload = json.loads(path.read_text())
    return payload.get("mode", path.stem), payload["cases"]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with", dest="with_path", type=Path, required=True)
    parser.add_argument("--without", dest="without_path", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="Dump per-case judgements to JSON.")
    args = parser.parse_args()

    api_key = os.environ.get("EMBER_TEST_LLM_API_KEY")
    if not api_key:
        # Fall back to the project .env so the script works without
        # the user manually exporting credentials each session.
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("EMBER_TEST_LLM_") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)
            api_key = os.environ.get("EMBER_TEST_LLM_API_KEY")
    if not api_key:
        print("EMBER_TEST_LLM_API_KEY not set", file=sys.stderr)
        return 2
    base_url = os.environ.get("EMBER_TEST_LLM_BASE_URL", "https://api.minimax.io/v1").rstrip("/")
    model_id = os.environ.get("EMBER_TEST_LLM_MODEL", "MiniMax-M2.7")

    runs = {
        "WITH": args.with_path,
        "WITHOUT": args.without_path,
    }
    cases_per_mode = {mode: load_run(p)[1] for mode, p in runs.items()}

    judgements: list[CaseJudgement] = []
    async with httpx.AsyncClient() as client:
        # Sequential — keeps API rate predictable, total ~10 calls.
        for mode, cases in cases_per_mode.items():
            for c in cases:
                case = c["case"]
                print(f"  judging {mode:8s} {case['name']}...", file=sys.stderr)
                j = await judge_one(
                    client,
                    base_url=base_url,
                    api_key=api_key,
                    model_id=model_id,
                    case_name=case["name"],
                    mode=mode,
                    task_input=case["input"],
                    expected_output=case.get("expected_output", ""),
                    judge_guidelines=case.get("judge_guidelines", ""),
                    response_text=c.get("response_text", ""),
                )
                judgements.append(j)

    # Side-by-side print.
    print()
    print(f"{'case':46s}  {'A loc':>6s} {'B reuse':>8s} {'C !dup':>7s}  hall  overall")
    case_names = sorted({j.case_name for j in judgements})
    for name in case_names:
        for mode in ("WITH", "WITHOUT"):
            j = next((x for x in judgements if x.case_name == name and x.mode == mode), None)
            if j is None:
                continue
            hall = len(j.hallucinations)
            print(
                f"{name[:44]:46s}  "
                f"{j.located.score:>6d} {j.reused.score:>8d} {j.not_duplicated.score:>7d}  "
                f"{hall:>4d}  {j.overall} ({mode})"
            )

    # Aggregate per-mode totals.
    print()
    for mode in ("WITH", "WITHOUT"):
        rows = [j for j in judgements if j.mode == mode]
        if not rows:
            continue
        a = sum(r.located.score for r in rows)
        b = sum(r.reused.score for r in rows)
        c = sum(r.not_duplicated.score for r in rows)
        hall = sum(len(r.hallucinations) for r in rows)
        passes = sum(1 for r in rows if r.overall == "PASS")
        max_axis = 3 * len(rows)
        print(
            f"{mode:8s} totals: A={a}/{max_axis}  B={b}/{max_axis}  C={c}/{max_axis}  "
            f"hallucinations={hall}  overall={passes}/{len(rows)}"
        )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                [j.model_dump() for j in judgements],
                indent=2,
            )
        )
        print(f"\nWrote per-case judgements to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
