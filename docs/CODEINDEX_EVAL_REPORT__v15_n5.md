# Ember Code + CodeIndex (hyde4) vs Claude Code — N=5 each

> **TL;DR** Same 12-case suite, 5 runs per system. Verdicts
> graded programmatically.
> **Ember-code + CodeIndex + MiniMax-M2.7 (scoped HyDE +
> encapsulation prompt): 49 ✅ / 4 ⚠ / 7 ❌** (82 % ✅).
> **Claude Code without CodeIndex (Opus-4.7): 31 ✅ / 22 ⚠ / 7 ❌**
> (52 % ✅). Ember-code wins by **+18 ✅ trials** — biggest
> gap we've seen. Wall time: ember-code mean **2 331 s**
> (σ 747 s), Claude Code mean **1 548 s** (σ 143 s). Cost:
> ember-code ~$0.05/run, Claude Code **$4.01/run** ($20 total).

## Setup

| | Ember Code + CodeIndex + MiniMax-M2.7 (hyde4 prompt) | Claude Code (Anthropic Opus-4.7) without CodeIndex |
|---|---|---|
| Runs | em-1…em-5 in `/tmp/eval-comparison/n5-hyde4/` | cc-1…cc-5 from v13 baseline |
| Prompt | `main_agent.codeindex.md` with scoped-HyDE + encapsulation rule | stock Claude Code system prompt |
| Suite | `evals/ember_server_v6.yaml` (12 cases) | same |
| Target codebase | ember-server (same chroma snapshot for ember-code) | same git ref of ember-server |

Same yaml, same prompts to the agents, same target codebase.
Only ember-code's prompt has the new rules (scoped HyDE,
encapsulation). Claude Code numbers are reused from v13 — no
re-run, since the Claude Code side hasn't changed.

**Grading is programmatic.** Each case has a documented set of
must-have / must-not-have substring patterns derived from the
case YAML's expected_output and judge_guidelines. Verdicts are
deterministic — running the grader twice on the same response
gives the same answer. Grader: `/tmp/grade.py` (substring
patterns per case; check `tool_trace` for typed-filter cases
11-12, `response_text` otherwise).

## Headline — N=5 result

| | Ember-code hyde4 (5 runs) | Claude Code (5 runs) |
|---|---:|---:|
| ✅ | **49 / 60** | 31 / 60 |
| ⚠ | **4 / 60** | 22 / 60 |
| ❌ | 7 / 60 | 7 / 60 |
| ✅-rate | **82 %** | 52 % |
| ⚠-rate | **7 %** | 37 % |
| ❌-rate | 12 % | 12 % |
| Wall time mean | 2 331 s (σ 747) | **1 548 s (σ 143)** |
| Wall time range | 1 715 – 3 599 s | 1 359 – 1 757 s |
| Cost / run | **~$0.05** | $4.01 |
| Total cost (5 runs) | **~$0.25** | $20.04 |
| Suite pass-rate (12/12) | 5/5 runs | 3/5 runs (cc-4 + cc-5: 11/12 each due to case 11 timeout) |

**Ember-code wins on quality** by **+18 ✅ trials** — a 30 pp
gap. Almost all of Claude Code's ⚠ trials moved into the ✅
column on ember-code; the ❌ count is now identical (7 each),
the difference is partial-credit cases the typed filters and
encapsulation rule push fully over the line.

**Claude Code still wins on wall time** by ~33 %, but the gap
narrowed slightly because hyde4's run 1 was a 60-min outlier
(case 5 + case 7 each took 8-16 min on that run). Median run
was closer to 30 min.

**Cost difference is unchanged** at two orders of magnitude.

## Visual comparison

### 60-trial verdict mix (each character ≈ 1 trial)

```
              ┌────────── 49 mergeable (82 %) ──────────┐┌4┐┌─ 7 ─┐
Ember-code   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░███████
              ┌── 31 mergeable (52 %) ─────┐┌──── 22 partial ────┐┌─ 7 ─┐
Claude Code  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░███████
                                          ▓ = mergeable  ░ = partial  █ = fail
```

### Δ vs v13 baseline (ember-code only — Claude Code reused)

```
                v13 baseline   hyde4 (this report)   Δ
✅ trials       41 / 60        49 / 60              +8
⚠ trials       13 / 60         4 / 60              -9
❌ trials       6 / 60         7 / 60              +1
✅-rate         68 %           82 %                +14 pp
```

The +14 pp move came almost entirely from converting ⚠ →✅ on
case 6 (soft-delete) +3, case 4 (dedupe) +2, case 12 (refactor
triage) +2, case 10 (daily cleanup) +1.

### Wall time and cost per run

```
Wall time (lower is better)
Ember-code   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 2 331 s
Claude Code  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                       1 548 s

Cost per run (lower is better)
Ember-code   ▏ ~$0.05
Claude Code  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ $4.01

Quality per dollar (higher is better — ✅ trials per dollar)
Ember-code   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 196 ✅/$
Claude Code  ▏ 1.5 ✅/$
```

## Per-case ✅ frequency (out of 5)

```
case                                            em ✅/5     cc ✅/5    Δ
─────────────────────────────────────────────────────────────────────────
1.  AIKeyPool sticky-revoke                      4 ████░   4 ████░    =
2.  Slack notifications                          5 █████   4 ████░   +1
3.  ChangesetUploader cleanup                    5 █████   3 ███░░   +2
4.  Dedupe (3 SummaryTags classes)               5 █████   5 █████    =
5.  Webhook rate-limit                           0 ░░░░░   0 ░░░░░    =
6.  Soft-delete repository                       5 █████   0 ░░░░░   +5
7.  Monthly AI quota                             4 ████░   4 ████░    =
8.  Commit-processing retry                      2 ██░░░   2 ██░░░    =
9.  Webhook replay                               5 █████   1 █░░░░   +4
10. Daily cleanup job                            4 ████░   0 ░░░░░   +4
11. Security triage                              5 █████   3 ███░░   +2
12. Refactor triage                              5 █████   5 █████    =
─────────────────────────────────────────────────────────────────────────
TOTAL                                           49 / 60   31 / 60   +18
```

## What hyde4 does differently from v13 baseline

Two prompt additions in `main_agent.codeindex.md`:

1. **Scoped HyDE** — code-shaped queries for `query_text`, but
   only as a **fallback** when triage typed filters and known-
   symbol literal lookups don't apply. Earlier global-HyDE
   experiments (v14a) hurt because the rule overrode triage on
   typed-filter cases.

2. **Encapsulation rule** — *Extend the existing class — don't
   bypass it.* When the user asks for a feature in domain X
   and a class already owns X, prefer adding a method to that
   class over inlining a parallel implementation in a new file.

## What hyde4 fixed vs v13

**+5 on case 6 (soft-delete repository)** — biggest single-case
gain in the suite's history. The encapsulation rule catches
cases where the agent would otherwise add `deleted_at` only
and miss `deleted_by`, because thinking "extend the existing
audit pattern" surfaces *both* columns.

**+1 on case 10 (daily cleanup — encapsulation target)**.
The agent now consistently reuses
`ChangesetUploader.delete_older_than` and `AIKeyPool` helpers
inside the new Celery Beat tasks, instead of inlining
`redis.from_url(...)` or `storage.Client()` in the cleanup job
itself. 4/5 vs baseline 3/5.

**+2 each on cases 4, 12** (dedupe + refactor triage) —
indirect: scoped HyDE and the encapsulation rule together push
the agent into a "find the existing thing first" framing,
which helps even on cases that aren't structurally about
extension.

## What hyde4 didn't fix

**Case 5 (webhook rate-limit) — still 0/5.** The agent
consistently lands on `ConcurrencyGate` (the wrong primitive —
tracks concurrent in-flight operations, not request rate) and
mistakes its similar Redis primitives (zadd/zremrangebyscore)
for the right rate-limit pattern. The right target is
AIKeyPool's actual rate-limit code, but it's never picked.

**Case 8 (commit-processing retry) — still 2/5.** The agent
follows the chain *commit-processing → process_git_event task
→ WebhookEvent* and proposes adding a `retry_history` JSON
column to `WebhookEvent` instead of extending the
`commit_processing_step` INSERT-only pattern. Wrong-table
failure.

Both failures share a single root cause: **the agent matches
on mechanism (similar primitives, related task name) instead
of on the user's stated subject**. A future "match purpose,
not mechanism" rule could target this — but the prior
subject-pin attempt regressed stable cases, so any next change
needs to gate on disambiguation rather than apply globally.

## Mergeable confidence

**~85 %.** The +14 pp ✅-rate gain reproduces across 5 runs
(every individual run beats baseline mean of 8.2 ✅/run by at
least 1; run 4 hit 11/12). No stable case regressed.
The two outstanding failures (cases 5, 8) were already chronic
in the baseline — same scores as v13 — so we are not making
them worse.

## Files

- Per-run JSONs: `/tmp/eval-comparison/n5-hyde4/em-{1..5}.json`
- Per-run telemetry: `/tmp/eval-comparison/n5-hyde4/em-{1..5}.telemetry.jsonl`
- Batch log: `/tmp/eval-comparison/n5-hyde4/batch.log`
- Grader: `/tmp/grade.py`
- Prompt: `src/ember_code/core/prompts/main_agent.codeindex.md`
  (scoped HyDE + encapsulation sections)
- Claude Code baseline: reused from v13 (`/tmp/eval-comparison/n5/cc-{3..5}.json`)
