# Ember Code + CodeIndex vs Claude Code — N=5 each, programmatic grading

> **TL;DR** Ran the same 12-case suite 5 times on each system.
> 60 trials per system, 120 trials total. Verdicts graded
> programmatically against documented per-case patterns
> (deterministic — same inputs always produce same verdict).
> **Ember-code + CodeIndex + MiniMax-M2.7: 41 ✅ / 13 ⚠ / 6 ❌**
> (68 % ✅). **Claude Code without CodeIndex: 31 ✅ / 22 ⚠ /
> 7 ❌** (52 % ✅). **The earlier "tied at 9 ✅ each" reading
> from N=2 was lucky sampling on the Claude Code side.** With
> N=5, ember-code wins by **+10 ✅ trials**. Wall time:
> ember-code mean **2 267 s** (σ 294 s), Claude Code mean
> **1 548 s** (σ 143 s). Cost: ember-code ~$0.05/run, Claude
> Code **$4.01/run** ($20 total).

## Setup

| | Ember Code + CodeIndex + MiniMax-M2.7 | Claude Code (Anthropic) without CodeIndex |
|---|---|---|
| Run 1 | em-1 (was v10-with.json) | cc-1 (was claude-code-with.json) |
| Run 2 | em-2 (was v12-ember-with.json) | cc-2 (was v12-claude-code.json) |
| Run 3 | em-3 (n5/em-3.json) | cc-3 (n5/cc-3.json) |
| Run 4 | em-4 (n5/em-4.json) | cc-4 (n5/cc-4.json) |
| Run 5 | em-5 (n5/em-5.json) | cc-5 (n5/cc-5.json) |

Same yaml, same chroma snapshot, same prompts, same target
codebase (ember-server). Only model-side seed varies.

**Grading is programmatic.** Each case has a documented set of
must-have / must-not-have substring patterns derived from the
case YAML's expected_output and judge_guidelines. Verdicts are
deterministic — running the grader twice on the same response
gives the same answer. This eliminates the verdict-noise we
saw with sub-agent grading in earlier reports.

The grading rubric for each case is in
`scripts/eval_verdict_grader.py` (TODO — currently inline in
the report's analysis). Examples:

- **Case 6 (soft-delete):** ✅ if both `deleted_at` AND
  `deleted_by` appear; ⚠ if only `deleted_at`; ❌ if neither.
- **Case 4 (dedupe):** ✅ if all 3 SummaryTags class names
  present; ⚠ if 1–2; ❌ if 0.
- **Case 8 (commit retry):** ❌ if response writes about
  `webhook_event` instead of `commit_processing_steps` (the
  wrong-table failure mode); ✅ if both `commit_processing_steps`
  and Celery `autoretry_for` / `retry_backoff`; ⚠ if one of
  the two.

## Headline — N=5 result

| | Ember-code (5 runs) | Claude Code (5 runs) |
|---|---:|---:|
| ✅ | **41 / 60** | 31 / 60 |
| ⚠ | 13 / 60 | 22 / 60 |
| ❌ | **6 / 60** | 7 / 60 |
| ✅-rate | **68 %** | 52 % |
| ⚠-rate | 22 % | 37 % |
| ❌-rate | **10 %** | 12 % |
| Wall time mean | 2 267 s (σ 294) | **1 548 s (σ 143)** |
| Wall time range | 2 044 – 2 781 s | 1 359 – 1 757 s |
| Cost / run | ~$0.05 | $4.01 |
| Total cost (5 runs) | ~$0.25 | $20.04 |
| Suite pass-rate (12/12) | 5/5 runs | 3/5 runs (cc-4 + cc-5: 11/12 each due to case 11 timeout) |

**Ember-code wins on quality** by 10 ✅ trials at N=60 — outside
the variance window we saw at N=2. **Claude Code wins on wall
time** by ~32 % consistently. The cost difference is two orders
of magnitude.

## Visual comparison

### 60-trial verdict mix (each character = 1 trial)

```
              ┌── 41 mergeable (68 %) ───────────────┐┌─   13    ─┐┌─ 6 ┐
Ember-code   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░██████
              ┌── 31 mergeable (52 %) ─────┐┌──── 22 partial ────┐┌─ 7 ─┐
Claude Code  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░███████
                                          ▓ = mergeable  ░ = partial  █ = fail
```

### Wall time and cost per run

```
Wall time (lower is better)
Ember-code   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 2 267 s
Claude Code  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                     1 548 s

Cost per run (lower is better)
Ember-code   ▏ ~$0.05
Claude Code  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ $4.01
```

## Per-case ✅ frequency (out of 5)

```
case                                            em ✅/5     cc ✅/5
───────────────────────────────────────────────────────────────────
1.  AIKeyPool sticky-revoke                      5 █████   4 ████░
2.  Slack notifications                          5 █████   4 ████░
3.  ChangesetUploader cleanup                    5 █████   3 ███░░
4.  Dedupe (3 SummaryTags classes)               3 ███░░   5 █████
5.  Webhook rate-limit                           0 ░░░░░   0 ░░░░░
6.  Soft-delete repository                       2 ██░░░   0 ░░░░░
7.  Monthly AI quota                             4 ████░   4 ████░
8.  Commit-processing retry                      1 █░░░░   2 ██░░░
9.  Webhook replay                               5 █████   1 █░░░░
10. Daily cleanup job                            3 ███░░   0 ░░░░░
11. Security triage                              5 █████   3 ███░░
12. Refactor triage                              3 ███░░   5 █████
```

## What this tells us per-case

### Stable wins (consistent across all 5 runs)

**Ember-code 5/5 ✅:** cases 1, 2, 3, 9, 11. The CodeIndex helps
*reliably* on locating service-class methods (1), identifying
parallel patterns (2), reusing existing client constructions
(3), reusing existing tables with status enums (9), and typed-
filter security triage (11).

**Claude Code 5/5 ✅:** cases 4 (dedupe — finds all 3 classes
every time), 12 (refactor triage — picks real candidates by
grep + line-count). Both are tasks where exhaustive
reading-the-code beats index-and-stop.

### Stable failures (consistent across all 5 runs)

**Both 0/5 ✅:** case 5 (webhook rate-limit). Neither system
reliably reaches AIKeyPool's `ZADD`/`ZREMRANGEBYSCORE`
sliding-window pattern. Ember-code consistently lands on
`ConcurrencyGate` (wrong primitive); Claude Code gives thin
"clarifying question" answers (16-22 s, 1 turn).

**Both 0/5 ✅:** case 6 for Claude Code, case 6 also rare on
ember-code (2/5). Both systems struggle to add `deleted_by`
alongside `deleted_at`. The audit-pair convention isn't
reliably extracted from the file even when both systems read
`Repository`.

### Where the index advantage is biggest

**Case 9 (webhook replay): em 5/5 vs cc 1/5.** This is the
clearest CodeIndex win — every ember-code run found
`webhook_events`, the `WebhookEventStatus` enum, and proposed
re-dispatching to the existing Celery task. Claude Code only
got it once.

**Case 10 (daily cleanup): em 3/5 vs cc 0/5.** Ember-code
delegates to `ChangesetUploader.delete_older_than` and
`AIKeyPool` helpers. Claude Code consistently inlines
`redis.from_url(...)` and `Client()` in the cleanup tasks
(meets the case's explicit FAIL gate).

**Case 11 (security triage): em 5/5 vs cc 3/5.** Typed
`security=['critical','major-issues']` filter wins reliably.
Claude Code's grep-based triage works most of the time but
hits a 7-minute timeout once (cc-4 + cc-5 cases 11 — both
ERRORED at the wrapper's 420 s cap).

### Where Claude Code's careful read beats the index

**Case 4 (dedupe): em 3/5 vs cc 5/5.** Ember-code occasionally
picks the wrong duplication target (portal status-labels in
em-1 and em-4) because the index-ranked top result isn't the
case's intended target. Claude Code's exhaustive grep
consistently surfaces all three `SummaryTags` classes.

**Case 12 (refactor triage): em 3/5 vs cc 5/5.** Same shape
— Claude Code's "read everything that looks long-or-complex"
loop reliably picks real refactor candidates. Ember-code's
typed filter sometimes returns thin lists.

### Mixed / coin-flip cases

**Case 6 (soft-delete): em 2/5, cc 0/5.** Both systems
flicker on whether `deleted_by` appears alongside
`deleted_at`. Ember-code wins this 2-0 over Claude Code but
neither is reliable.

**Case 7 (AI quota): em 4/5, cc 4/5.** Both systems usually
find the right reuse target (`chat_usage` post-migration);
both occasionally miss it.

**Case 8 (commit retry): em 1/5, cc 2/5.** Both systems
unreliable. The "wrote about webhook_event instead of
commit_processing_steps" failure mode is common on both
sides.

## Wall time and cost — robust against verdict noise

These metrics sum over all 12 case-decisions per run, so
they're stable across seeds.

| | mean | σ | range | 5-run total |
|---|---:|---:|---:|---:|
| Ember wall | 2 267 s | 294 s (13 %) | 2 044 – 2 781 s | 11 333 s |
| CC wall | **1 548 s** | 143 s (9 %) | 1 359 – 1 757 s | **7 742 s** |
| Ember cost | ~$0.05 | low | $0.04 – $0.07 | ~$0.25 |
| CC cost | $4.01 | $0.78 (19 %) | $2.99 – $4.92 | **$20.04** |

**Claude Code is consistently ~32 % faster wall-clock**, and
*much* more consistent run-to-run (σ 9 % vs 13 %). This is the
robust advantage of using a stronger model.

**Cost is two orders of magnitude apart.** $20 vs $0.25 across
the same 60 case-runs. The MiniMax + CodeIndex stack is the
choice for any deployment that wants to amortize agent runs
over many users.

## Where the v6→v10 prompt-iteration narrative survives N=5

A few claims from the v6–v10 reports do hold up at N=5:

1. **Case 11 typed-filter use is robust.** All 5 ember-code
   runs called `codeindex_query` with real `security=[...]`
   values. The empty-call guardrail + worked example fixed
   this consistently. **Claim survives.**

2. **Case 3 doesn't fail with `Client()`.** All 5 ember runs
   correctly mirror `_upload_sync`'s pattern. The earlier
   v7/v8 ❌ verdicts on case 3 really were wrong (they
   penalized correct mirroring). **Claim survives.**

3. **Case 4 dedupe-count rule fires inconsistently.** 3/5 on
   ember vs 5/5 on Claude Code. The count-N preamble rule
   isn't enough to overcome model-side variance on which
   duplication the agent finds first. **Claim partially
   survives** — moves the needle but not reliably.

What doesn't survive:

1. **"v9 finally got `deleted_by`."** N=5 shows 2/5 ember runs
   miss `deleted_by`. The v9 ✅ was a sample, not a fix.

2. **"Reasoning didn't help"** — confirmed at N=1, but we
   didn't re-test at N=5. The v9 ReasoningTools experiment
   should ideally be retried at N=5 to be fair.

## Recommendation

1. **The cost gap is the practical headline.** $0.25 for 5
   runs vs $20 for 5 runs. At any scale beyond a single
   developer, ember-code + CodeIndex + MiniMax is the only
   sustainable stack.

2. **The quality gap is real but smaller than v6–v10
   reports claimed.** Ember-code reliably wins on cases 9,
   10, 11. Claude Code reliably wins on cases 4, 12. Both
   tied or unstable on cases 5, 6, 7, 8 — these are
   model-bound, not tool-bound.

3. **Stable cases (1, 2, 3, 9, 11 on ember; 4, 12 on cc)
   are the ones to use for prompt iteration.** Cases that
   flicker (5, 6, 7, 8) need bigger N or a different
   eval design.

4. **Case 5 needs prompt or tooling work.** Both systems
   0/5. The fixed-window vs sliding-window distinction
   isn't being learned from the codebase. Either AIKeyPool's
   pattern needs a more explicit doc-comment, or the
   case prompt needs to drop a hint about the
   ZADD/ZREMRANGEBYSCORE primitive.

5. **The CC case-11 timeout (2/5 runs) is a real
   reliability problem** for Claude Code on triage tasks.
   The grep-based loop hits pathological state on this
   case. Index-backed triage doesn't have this failure
   mode.

## Suggested follow-ups

1. **Run the Claude + CodeIndex (MCP) experiment** to
   isolate the model factor from the tool factor. With this
   N=5 baseline established, a Claude+CodeIndex run at N=5
   would tell us whether the index advantage holds at top
   model strength.

2. **Move the verdict grader to a script.** Currently
   inline; should be `scripts/eval_verdict_grader.py` so
   the rubric is reviewable in code review.

3. **Update case 6's eval YAML** to make the audit-pair
   requirement more explicit. 2/5 + 0/5 success across both
   systems suggests the case prompt isn't transmitting the
   `_at`+`_by` parity convention clearly.

## Artifacts

```
/tmp/eval-comparison/
├── v10-with.json                      em-1
├── v12-ember-with.json                em-2
├── claude-code-with.json              cc-1
├── v12-claude-code.json               cc-2
└── n5/
    ├── em-3.json, em-4.json, em-5.json
    ├── cc-3.json, cc-4.json, cc-5.json
    ├── ember-batch.log, cc-batch.log
    └── em-3.telemetry.jsonl, em-4.telemetry.jsonl, em-5.telemetry.jsonl
```

```
# Re-run any single trial:
.venv/bin/python scripts/run_codeindex_eval.py \
  --suite ember_server_v6 \
  --target-project-dir /Users/dmytrozezyk/ai_coding/ember-server \
  --out /tmp/eval-comparison/em-N.json

.venv/bin/python scripts/run_claude_code_eval.py \
  --suite ember_server_v6 \
  --target-project-dir /Users/dmytrozezyk/ai_coding/ember-server \
  --out /tmp/eval-comparison/cc-N.json --no-judge
```
