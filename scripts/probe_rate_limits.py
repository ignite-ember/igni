"""Probe MiniMax rate-limit behaviour across concurrency × prompt-size.

For each prompt size in ``--sizes`` and concurrency level in
``--levels``, fires that many requests in parallel against the same
endpoint the smoke eval uses (``EMBER_TEST_LLM_*`` env vars), then
reports per-cell counts and latency stats. Stops escalating
concurrency on a given size once we see sustained throttling
(rate-limited fraction ≥ ``--stop-rl-pct``) so we don't keep banging
on the wall after the answer is clear.

Outcomes per request:

* ``ok``        — 2xx with non-empty content
* ``rate``      — HTTP 429, or a 4xx/5xx body that mentions ``rate`` /
  ``limit`` / ``throttle`` / ``quota``. MiniMax returns rate-limit
  signals through several shapes in practice; widening the matcher
  keeps the probe honest.
* ``timeout``   — exceeded ``--per-req-timeout`` seconds
* ``conn``      — pre-response transport error (DNS, TCP reset, ...)
* ``other``     — non-2xx without a rate-limit signal

The judge prompt is intentionally trivial (``reply with READY`` and
``max_tokens=1``) so output cost is constant — only the **input**
prompt size varies, which is what we want to measure.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


def _load_dotenv() -> None:
    p = Path(".env")
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _make_prompt(target_tokens: int) -> str:
    """Build a prompt of approximately ``target_tokens`` tokens.

    Uses tiktoken for an accurate count when available — falls back to
    the well-known ~4 chars/token rule otherwise. We want this padding
    deterministic: each call produces the same prompt for a given size
    so latency variation reflects the server, not the input.
    """
    instruction = (
        "Read the context below. Reply with the single word READY.\n\n"
        "<context>\n"
    )
    suffix = "\n</context>\n\nReply: "
    overhead = 0  # filled in below

    base_para = (
        "The quick brown fox jumps over the lazy dog. "
        "Pack my box with five dozen liquor jugs. "
        "Sphinx of black quartz, judge my vow. "
    )

    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        overhead = len(enc.encode(instruction + suffix))
        # Scale paragraphs until we hit the target.
        body = ""
        cur_tokens = 0
        unit_tokens = len(enc.encode(base_para))
        if unit_tokens == 0:
            unit_tokens = max(1, len(base_para) // 4)
        # +overhead makes the *total* prompt hit the target.
        needed = max(0, target_tokens - overhead)
        repeats = max(0, needed // unit_tokens)
        body = base_para * repeats
        cur_tokens = len(enc.encode(body))
        # Fine-tune: append/trim short strings until we land within ±2%.
        while cur_tokens < needed - max(2, needed * 0.02):
            body += base_para
            cur_tokens = len(enc.encode(body))
        return instruction + body + suffix
    except ImportError:
        # Char-based fallback. ~4 chars/token is the standard rule of
        # thumb for English; close enough for an order-of-magnitude probe.
        chars = max(0, target_tokens * 4 - len(instruction) - len(suffix))
        repeats = max(1, chars // len(base_para))
        return instruction + (base_para * repeats) + suffix


@dataclass
class Outcome:
    kind: str  # ok | rate | timeout | conn | other
    latency_ms: float
    detail: str = ""


_RATE_HINTS = ("rate", "limit", "throttle", "quota", "too many", "tps")


def _classify_status_body(status: int, body: str) -> str:
    if status == 429:
        return "rate"
    if status >= 400:
        low = body.lower()
        if any(h in low for h in _RATE_HINTS):
            return "rate"
        return "other"
    return "ok"


async def _one_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    payload: dict,
    per_req_timeout: float,
) -> Outcome:
    t0 = time.perf_counter()
    try:
        r = await client.post(
            url, json=payload, headers=headers, timeout=per_req_timeout
        )
    except httpx.TimeoutException as exc:
        return Outcome("timeout", (time.perf_counter() - t0) * 1000, str(exc))
    except (httpx.ConnectError, httpx.ReadError, httpx.WriteError) as exc:
        return Outcome("conn", (time.perf_counter() - t0) * 1000, str(exc))
    except httpx.HTTPError as exc:
        return Outcome("conn", (time.perf_counter() - t0) * 1000, repr(exc))

    elapsed_ms = (time.perf_counter() - t0) * 1000
    text = r.text
    kind = _classify_status_body(r.status_code, text)
    if kind == "ok":
        # Any error inside a 2xx body? MiniMax sometimes returns 200
        # with ``base_resp.status_code != 0``.
        try:
            data = r.json()
            base = data.get("base_resp", {}) if isinstance(data, dict) else {}
            if isinstance(base, dict) and base.get("status_code", 0) != 0:
                msg = str(base.get("status_msg", ""))
                low = msg.lower()
                if any(h in low for h in _RATE_HINTS):
                    return Outcome("rate", elapsed_ms, msg)
                return Outcome("other", elapsed_ms, msg)
        except Exception:
            pass
        return Outcome("ok", elapsed_ms)

    return Outcome(kind, elapsed_ms, text[:200])


def _fmt_pct(num: int, total: int) -> str:
    if total == 0:
        return "  -  "
    return f"{100 * num // total:>3d}%"


def _fmt_ms(values: list[float]) -> str:
    if not values:
        return "    -"
    p50 = statistics.median(values)
    if len(values) >= 20:
        p99 = sorted(values)[max(0, int(len(values) * 0.99) - 1)]
    else:
        p99 = max(values)
    return f"p50={p50/1000:5.1f}s p99={p99/1000:5.1f}s"


async def _run_cell(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    model: str,
    prompt: str,
    concurrency: int,
    per_req_timeout: float,
) -> list[Outcome]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1,
        "temperature": 0,
    }
    tasks = [
        asyncio.create_task(
            _one_request(client, url, headers, payload, per_req_timeout)
        )
        for _ in range(concurrency)
    ]
    return await asyncio.gather(*tasks)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sizes",
        default="1000,10000,25000,50000,75000,100000",
        help="comma-separated prompt token sizes to probe",
    )
    parser.add_argument(
        "--levels",
        default="1,2,4,8,16,32",
        help="comma-separated concurrency levels per size",
    )
    parser.add_argument(
        "--per-req-timeout",
        type=float,
        default=120.0,
        help="hard timeout per individual request (seconds)",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=5.0,
        help="seconds to sleep between cells so the previous burst's "
        "rate-limit window can clear",
    )
    parser.add_argument(
        "--stop-rl-pct",
        type=int,
        default=50,
        help="stop escalating concurrency at this size once "
        "rate-limited fraction reaches this percent",
    )
    args = parser.parse_args()

    _load_dotenv()
    if not os.getenv("EMBER_TEST_LLM_API_KEY"):
        print("Set EMBER_TEST_LLM_API_KEY (in .env or env).", file=sys.stderr)
        return 1

    base_url = os.getenv("EMBER_TEST_LLM_BASE_URL") or "https://api.openai.com/v1"
    model = os.getenv("EMBER_TEST_LLM_MODEL") or "gpt-4o-mini"
    api_key = os.environ["EMBER_TEST_LLM_API_KEY"]

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    levels = [int(s) for s in args.levels.split(",") if s.strip()]

    print(f"Endpoint: {base_url}")
    print(f"Model:    {model}")
    print(f"Sizes:    {sizes}")
    print(f"Levels:   {levels}")
    print(f"Per-req timeout: {args.per_req_timeout}s | Cooldown: {args.cooldown}s")
    print()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Single AsyncClient with generous limits so the client itself
    # isn't the bottleneck — we want the *server* to be the bottleneck.
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    timeout = httpx.Timeout(args.per_req_timeout)

    print(
        f"{'size':>8s}  {'conc':>5s}  {'ok':>4s} {'rate':>4s} {'tout':>4s} "
        f"{'conn':>4s} {'oth':>4s}  {'%ok':>4s}  {'latency':<22s}"
    )
    print("-" * 78)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        for tok_size in sizes:
            prompt = _make_prompt(tok_size)
            actual_chars = len(prompt)
            for conc in levels:
                outcomes = await _run_cell(
                    client,
                    f"{base_url}/chat/completions",
                    headers,
                    model,
                    prompt,
                    conc,
                    args.per_req_timeout,
                )
                kinds = [o.kind for o in outcomes]
                ok = kinds.count("ok")
                rl = kinds.count("rate")
                to = kinds.count("timeout")
                cn = kinds.count("conn")
                ot = kinds.count("other")
                ok_lat = [o.latency_ms for o in outcomes if o.kind == "ok"]

                size_label = f"{tok_size//1000}k" if tok_size >= 1000 else str(tok_size)
                print(
                    f"{size_label:>8s}  {conc:>5d}  {ok:>4d} {rl:>4d} {to:>4d} "
                    f"{cn:>4d} {ot:>4d}  {_fmt_pct(ok, conc):>4s}  {_fmt_ms(ok_lat)}"
                )
                # Spit a one-line sample of any non-ok detail so we know
                # *how* MiniMax is signalling — useful in the trace.
                first_bad = next((o for o in outcomes if o.kind != "ok"), None)
                if first_bad is not None and first_bad.detail:
                    snippet = first_bad.detail.replace("\n", " ")[:120]
                    print(f"            └─ {first_bad.kind}: {snippet}")

                # Bail on this size if the server is clearly throttling.
                if conc > 1 and rl * 100 >= args.stop_rl_pct * conc:
                    print(
                        f"            └─ stop: rate-limited ≥ {args.stop_rl_pct}% "
                        f"at conc={conc}, skipping higher levels for this size"
                    )
                    break

                if args.cooldown > 0:
                    await asyncio.sleep(args.cooldown)
            # Note: the prompt's actual size varied slightly from the
            # target due to tokenizer mismatch — print so we know.
            print(f"            └─ prompt size: {actual_chars} chars")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
