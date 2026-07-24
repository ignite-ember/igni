"""Drive the BackendServer directly and time every yielded event.

Reproduces the "agent stops talking but queue stays visible" + the
"input lag after a few edits" symptoms by sending N messages through
the *real* backend with the user's real cloud credentials and the
configured default model. Records:

* Time from message send → first event yielded
* Time from message send → first ``ModelChunk`` (real content)
* Time from message send → last ``ModelChunk`` (visible end)
* Time from last ``ModelChunk`` → stream close ("the tail")
* Event-by-event timing + type histogram

The tail is the interesting bit — if the FE keeps ``_processing = True``
until the stream closes, anything in the tail directly maps to user-
visible queue lag.

Run with::

    .venv/bin/python scripts/profile_run.py \\
        --project ~/PycharmProjects/isora/code-index-test \\
        --runs 3 \\
        --prompt "summarize what you see in this directory" \\
        --output /tmp/profile_run.jsonl

The script reuses ``~/.ember/credentials.json`` so the same model the
user sees in the TUI (MiniMax-M2.7 etc.) is exercised end-to-end.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import logging
from pathlib import Path
import statistics
import sys
import time

# Repo-local import without install
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ember_code.backend.server import BackendServer  # noqa: E402
from ember_code.core.config.settings import load_settings  # noqa: E402

logger = logging.getLogger(__name__)

# Event types that carry actual model-generated content (vs. metadata).
# Used to define "the tail": time from the last content-bearing event to
# stream close. Names map to classes in ``ember_code.protocol.messages``.
_CONTENT_EVENTS = {"ContentDelta", "ToolCompleted"}


async def _ensure_cloud_models(settings) -> None:
    """Mirror the TUI startup that pulls hosted models into the registry."""
    from ember_code.core.auth.credentials import CloudCredentials
    from ember_code.core.config.cloud_models import CloudModelCatalogClient

    token = CloudCredentials(settings.auth.credentials_file).access_token
    if not token:
        raise RuntimeError(
            "No cloud token found. Run `/login` in ember-code to authenticate first."
        )
    client = CloudModelCatalogClient(settings.api_url, token)
    loop = asyncio.get_event_loop()
    fetch_result = await loop.run_in_executor(None, client.fetch)
    if not fetch_result.ok or not fetch_result.entries:
        raise RuntimeError(
            f"Cloud-model discovery returned no entries "
            f"(api_url={settings.api_url}, reason={fetch_result.reason.value}). "
            "Make sure the API is reachable and your credentials are valid."
        )
    client.merge_into(settings.models.registry, entries=fetch_result.entries)
    logger.info(
        "loaded %d cloud model(s) from %s", len(fetch_result.entries), settings.api_url
    )


async def _profile_raw_agno(
    settings,
    project_dir: Path,
    prompt: str,
    verbose: bool = False,
) -> dict:
    """Drive Agno's team stream directly to see EVERY raw Agno event.

    The BackendServer wrapper serialises Agno events into our protocol
    messages and drops anything that doesn't map cleanly — including
    some metadata events that fire in the post-stream tail. Going
    straight to Agno reveals what the model SDK actually emits between
    ``ModelRequestCompleted`` (visible response done) and the stream
    closing, which is the gap we're investigating.
    """
    from ember_code.core.session import Session

    sess = Session(settings, project_dir=project_dir)
    team = sess.main_team
    events: list[dict] = []
    histogram: Counter[str] = Counter()
    last_content_idx = -1

    send_t = time.monotonic()
    last_event_t = send_t
    async for event in team.arun(prompt, stream=True):
        now = time.monotonic()
        offset = now - send_t
        gap = now - last_event_t
        last_event_t = now
        kind = type(event).__name__
        events.append({"i": len(events), "t": offset, "type": kind, "gap": gap})
        histogram[kind] += 1
        # Agno's content events are RunContentEvent / ToolCallCompletedEvent.
        if kind.startswith("RunContent") or kind.startswith("ToolCallCompleted"):
            last_content_idx = len(events) - 1
        if verbose:
            print(f"  [{offset:>7.3f}s  +{gap:>5.3f}] {kind}")
    close_t = time.monotonic()
    total = close_t - send_t

    t_last_content = events[last_content_idx]["t"] if last_content_idx >= 0 else None
    tail = (total - t_last_content) if t_last_content is not None else None

    return {
        "mode": "raw_agno",
        "total_s": round(total, 3),
        "time_to_last_content_s": round(t_last_content, 3) if t_last_content else None,
        "tail_s": round(tail, 3) if tail is not None else None,
        "events_total": len(events),
        "histogram": dict(histogram),
        "tail_events": (
            [{"i": e["i"], "t": round(e["t"], 3), "type": e["type"], "gap": round(e["gap"], 3)} for e in events[last_content_idx + 1 :]]
            if last_content_idx >= 0
            else []
        ),
    }


async def _profile_one_run(
    backend: BackendServer,
    prompt: str,
    run_index: int,
    verbose: bool = False,
) -> dict:
    """Execute one ``run_message`` and capture per-event timings.

    Returns a dict suitable for JSONL emission with all timings in
    seconds since send and a per-type histogram of events.
    """
    events: list[dict] = []
    histogram: Counter[str] = Counter()
    last_content_idx = -1
    first_event_idx = -1

    send_t = time.monotonic()
    last_event_t = send_t
    async for proto in backend.run_message(prompt):
        now = time.monotonic()
        offset = now - send_t
        gap = now - last_event_t
        last_event_t = now
        kind = type(proto).__name__
        events.append({"i": len(events), "t": offset, "type": kind, "gap": gap})
        histogram[kind] += 1
        if first_event_idx < 0:
            first_event_idx = len(events) - 1
        if kind in _CONTENT_EVENTS:
            last_content_idx = len(events) - 1
        if verbose:
            print(f"  [{offset:>7.3f}s  +{gap:>5.3f}] {kind}")

    close_t = time.monotonic()
    total = close_t - send_t

    t_first = events[first_event_idx]["t"] if first_event_idx >= 0 else None
    t_last_content = events[last_content_idx]["t"] if last_content_idx >= 0 else None
    tail = (total - t_last_content) if t_last_content is not None else None

    return {
        "run_index": run_index,
        "prompt": prompt,
        "total_s": round(total, 3),
        "time_to_first_event_s": round(t_first, 3) if t_first is not None else None,
        "time_to_last_content_s": round(t_last_content, 3) if t_last_content is not None else None,
        "tail_s": round(tail, 3) if tail is not None else None,
        "events_total": len(events),
        "histogram": dict(histogram),
        # Tail-event detail: list every event AFTER the last content
        # event so we can see exactly what's blocking the stream close.
        "tail_events": (
            [
                {"i": e["i"], "t": round(e["t"], 3), "type": e["type"]}
                for e in events[last_content_idx + 1 :]
            ]
            if last_content_idx >= 0
            else []
        ),
    }


def _summarize(records: list[dict]) -> dict:
    """Compute aggregate stats across runs."""

    def _stats(values: list[float]) -> dict:
        if not values:
            return {"n": 0}
        return {
            "n": len(values),
            "min": round(min(values), 3),
            "max": round(max(values), 3),
            "mean": round(statistics.mean(values), 3),
            "median": round(statistics.median(values), 3),
        }

    return {
        "runs": len(records),
        "total_s": _stats([r["total_s"] for r in records if r.get("total_s") is not None]),
        "time_to_first_event_s": _stats(
            [r["time_to_first_event_s"] for r in records if r.get("time_to_first_event_s") is not None]
        ),
        "time_to_last_content_s": _stats(
            [r["time_to_last_content_s"] for r in records if r.get("time_to_last_content_s") is not None]
        ),
        "tail_s": _stats([r["tail_s"] for r in records if r.get("tail_s") is not None]),
        # Tail-event types aggregated across all runs — tells us *what*
        # the backend keeps emitting after the visible response is done.
        "tail_event_types": dict(
            Counter(
                ev["type"]
                for r in records
                for ev in r.get("tail_events", [])
            )
        ),
    }


async def main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    project_dir = Path(args.project).expanduser().resolve()
    if not project_dir.is_dir():
        print(f"project dir not found: {project_dir}", file=sys.stderr)
        return 2

    settings = load_settings(project_dir=project_dir)
    if args.model:
        settings.models.default = args.model
    print(f"project_dir={project_dir}")
    print(f"api_url={settings.api_url}")
    print(f"default model (before discovery): {settings.models.default}")

    await _ensure_cloud_models(settings)
    print(f"registry size (after discovery):  {len(settings.models.registry)}")
    print(f"default model (after discovery):  {settings.models.default}")

    if args.raw:
        print("\n=== raw Agno profile (BackendServer wrapper skipped) ===")
        for i in range(args.runs):
            print(f"\n--- raw run {i + 1}/{args.runs} ---")
            r = await _profile_raw_agno(
                settings, project_dir, args.prompt, verbose=args.verbose_events
            )
            print(
                f"  total={r['total_s']}s  "
                f"last_content={r['time_to_last_content_s']}s  "
                f"tail={r['tail_s']}s  "
                f"events={r['events_total']}"
            )
            if r["tail_events"]:
                print(f"  tail events: {[e['type'] for e in r['tail_events']]}")
        return 0

    backend = BackendServer(settings, project_dir=project_dir)
    await backend.startup()

    records: list[dict] = []
    for i in range(args.runs):
        print(f"\n=== run {i + 1}/{args.runs} ===")
        record = await _profile_one_run(
            backend, args.prompt, i, verbose=args.verbose_events
        )
        records.append(record)
        print(
            f"  total={record['total_s']}s  "
            f"first_event={record['time_to_first_event_s']}s  "
            f"last_content={record['time_to_last_content_s']}s  "
            f"tail={record['tail_s']}s  "
            f"events={record['events_total']}"
        )
        if record["tail_events"]:
            tail_types = Counter(e["type"] for e in record["tail_events"])
            print(f"  tail events: {dict(tail_types)}")

    summary = _summarize(records)
    print("\n=== summary ===")
    print(json.dumps(summary, indent=2))

    if args.output:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
            fh.write(json.dumps({"_summary": summary}) + "\n")
        print(f"\nwrote per-run events to {out_path}")
    return 0


def _argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument(
        "--project",
        default=".",
        help="Project directory to use as ember-code's working dir.",
    )
    p.add_argument(
        "--prompt",
        default="Say hello in one short sentence.",
        help="Message to send each run. Keep it short to minimise model variance.",
    )
    p.add_argument(
        "--runs",
        type=int,
        default=3,
        help="How many times to repeat the prompt against the same backend.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override the default model (e.g. ``MiniMax-M2.7``). "
        "When omitted, uses whatever ``settings.models.default`` says.",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Optional JSONL output path (one record per run + final summary).",
    )
    p.add_argument(
        "--verbose-events",
        action="store_true",
        help="Print every event as it arrives — useful for the first run.",
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="Drive Agno's stream directly (skip BackendServer wrapper). "
        "Shows every Agno event including ones the wrapper filters.",
    )
    return p


if __name__ == "__main__":
    args = _argparser().parse_args()
    sys.exit(asyncio.run(main(args)))
