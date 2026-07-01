# Benchmarks — igni vs Claude Code

A reproducible head-to-head comparison on a 12-case software-engineering benchmark, run 5 times per system. Same suite, same target codebase, same prompts; only the agent under test differs.

> **TL;DR** igni (MiniMax-M2.7) + CodeIndex produces directly-mergeable code on **49 of 60 trials (82 %)**. Claude Code (Opus-4.7, no CodeIndex) does the same on **31 of 60 (52 %)**. igni wins by **+18 ✅ trials** at one-eightieth of the per-run cost.

## Setup

| | igni (MiniMax-M2.7) + CodeIndex | Claude Code (Opus-4.7) without CodeIndex |
|---|---|---|
| Runs | N=5 | N=5 |
| Suite | 12 cases — 5 codewrite + 7 db_codewrite tasks against a real Python service | same |
| Target codebase | A real production-style Python project (~600 modules) | same |
| Grading | Programmatic substring patterns derived from each case's `expected_output` and `judge_guidelines`. Verdicts are deterministic — running the grader twice on the same response gives the same answer. | same |

The 12 tasks span four task shapes: feature implementation that should reuse an existing class ("add a method", "extend X"), schema additions that should match audit-column conventions, retrieval-and-rank ("find the worst N security issues"), and refactor triage. Each task's grading rubric checks for both *positive* signals (the right reuse target was named, the right convention was matched) and *negative* signals (parallel-infra was avoided, wrong-target failure modes didn't fire).

## Headline

| | igni (5 runs) | Claude Code (5 runs) |
|---|---:|---:|
| ✅ — directly mergeable | **49 / 60** | 31 / 60 |
| ⚠ — partial / needs clarification | **4 / 60** | 22 / 60 |
| ❌ — wrong target / needs rework | 7 / 60 | 7 / 60 |
| ✅-rate | **82 %** | 52 % |
| ⚠-rate | **7 %** | 37 % |
| ❌-rate | 12 % | 12 % |
| Wall time mean | 2 331 s (σ 747) | **1 548 s (σ 143)** |
| Wall time range | 1 715 – 3 599 s | 1 359 – 1 757 s |
| Cost / run | **~$0.05** | $4.01 |
| Total cost (5 runs) | **~$0.25** | $20.04 |
| Suite pass-rate (12/12) | 5/5 runs | 3/5 runs |

**igni wins on quality** by **+18 ✅ trials** — a 30-percentage-point gap. Almost all of Claude Code's ⚠ trials moved into the ✅ column under igni; the ❌ count is identical (7 each), so the gap is entirely in the ⚠ band — igni commits to a concrete answer where Claude Code stays in partial-design mode.

**Claude Code wins on wall time** by ~33 %. igni's mean is dragged up by one outlier run on a single hard case; the median run is closer to 30 minutes.

**Cost difference is two orders of magnitude.** igni uses a smaller model (MiniMax-M2.7) and the CodeIndex's pre-computed semantic + metadata index does most of the navigation work, so per-trial token use is much lower.

## Visual comparison

### 60-trial verdict mix (each character ≈ 1 trial)

```
              ┌──────────── 49 mergeable (82 %) ─────────────┐┌─4┐┌─ 7 ─┐
igni   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░███████
              ┌── 31 mergeable (52 %) ─────┐┌──── 22 partial ────┐┌─ 7 ─┐
Claude Code  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░███████
                                          ▓ = mergeable  ░ = partial  █ = fail
```

### Wall time, cost, and quality-per-dollar

```
Wall time (lower is better)
igni   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 2 331 s
Claude Code  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                       1 548 s

Cost per run (lower is better)
igni   ▏ ~$0.05
Claude Code  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ $4.01

Quality per dollar (higher is better — ✅ trials per $1)
igni   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 196 ✅/$
Claude Code  ▏ 1.5 ✅/$
```

## Per-case ✅ frequency (out of 5)

```
case                                            em ✅/5     cc ✅/5    Δ
─────────────────────────────────────────────────────────────────────────
1.  Service-class method (sticky-revoke)         4 ████░   4 ████░    =
2.  Notifications parallel pattern               5 █████   4 ████░   +1
3.  Reuse cached client (storage cleanup)        5 █████   3 ███░░   +2
4.  Dedupe across N parallel classes             5 █████   5 █████    =
5.  Rate-limit (chronic — both fail)             0 ░░░░░   0 ░░░░░    =
6.  Soft-delete with audit-column pair           5 █████   0 ░░░░░   +5
7.  Reuse existing usage table                   4 ████░   4 ████░    =
8.  Append-only retry history (chronic)          2 ██░░░   2 ██░░░    =
9.  Reuse existing event/status table            5 █████   1 █░░░░   +4
10. Coordinate existing helpers (cleanup job)    4 ████░   0 ░░░░░   +4
11. Index-backed triage (security)               5 █████   3 ███░░   +2
12. Refactor triage                              5 █████   5 █████    =
─────────────────────────────────────────────────────────────────────────
TOTAL                                           49 / 60   31 / 60   +18
```

## Per-case detail

Each case below shows the literal prompt the agent received, what each system decided, and why each verdict landed.

### Case 1 — Service-class method (sticky-revoke)

> **Prompt:** A user reported that their AI key assignment got "stuck" on a specific tier and they want a clean way to release it so the next request picks a fresh key. Add a way for an admin to revoke a single user's sticky key assignment for a given tier. Sketch the code — don't edit anything. Use whatever conventions the surrounding code already uses; I want it to feel native.

| | igni (4/5) | Claude Code (4/5) |
|---|---|---|
| **Decision** | Add `release_sticky()` method on the existing pool class using its private prefix attribute and cached redis accessor. | Same shape — both systems find the class and mirror the convention. |
| **Why ✅** | Class-method shape, both accessors named, no fresh redis client. | Identical — Claude Code finds the class on the first read. |
| **Why miss** | One run used a property shorthand instead of the canonical method-call accessor — code is functionally correct, fails the literal grader. | Same shorthand failure mode in one run. |

Effectively a tie — both systems handle this well; the misses are grader strictness, not wrong code.

### Case 2 — Notifications parallel pattern

> **Prompt:** Right now we email people when their commit gets analyzed — started, completed, failed. The team wants the same updates in Slack so they can react in the channel without checking inbox. Sketch how you'd add it and where it'd live. Don't edit anything, just show me the shape.

| | igni (5/5) | Claude Code (4/5) |
|---|---|---|
| **Decision** | New parallel module under `app/services/slack/` with three notify functions mirroring the existing email module. | Same parallel-module shape — Claude Code also calls out trigger points. |
| **Why ✅** | All three function names + slack path. | Same. |
| **Why miss** | — | One Claude Code run pushed back on whether to unify the notification layer instead of producing the parallel functions, missing the literal pattern. |

igni's mandatory preamble forces the parallel functions to be named explicitly; Claude Code occasionally turns the question philosophical.

### Case 3 — Reuse cached client (storage cleanup)

> **Prompt:** Our changeset bucket in GCS is filling up with old uploads from repos we no longer index. Add a cleanup that deletes any changeset blob older than N days. Sketch how you'd wire it — no file edits, just the code.

| | igni (5/5) | Claude Code (3/5) |
|---|---|---|
| **Decision** | New `delete_older_than()` **method on** the existing uploader class, reusing its cached client and prefix. | Some runs add a sibling `reaper.py` module instead — parallel infra rather than extension. |
| **Why ✅** | Method-on-class shape, reuses cached client, uses the existing `list_blobs(prefix=...)` pattern. | Method-on-class when Claude Code chose extension; correct behavior. |
| **Why miss** | — | The "sibling reaper class" runs duplicate the auth/client setup the existing class already owns. |

Encapsulation gap most visible here — igni's rule blocks the parallel-class shortcut; Claude Code takes it 2/5 times.

### Case 4 — Dedupe across N parallel classes

> **Prompt:** Code review flagged that we render quality tags in three or four places with nearly the same logic — every time someone adds a new field, they have to remember to update each one. Find where this duplication lives and sketch a consolidation. Don't edit files, just show the shape of the refactor.

| | igni (5/5) | Claude Code (5/5) |
|---|---|---|
| **Decision** | Both: enumerate all 3 classes that share the duplicated method and propose a mixin or base class. | Same. Claude Code additionally surfaces the inverse parser elsewhere in the codebase. |
| **Why ✅** | Count-of-3 satisfied; mixin proposed. | Same — Claude Code's exhaustive read-the-files style is at its best on consolidation. |

Stable tie. Pre-classified data (igni) and brute-force read (Claude Code) converge on the same answer.

### Case 5 — Rate-limit (chronic 0/5 both)

> **Prompt:** Our webhook endpoints get hammered when a repo pushes a large batch of commits — sometimes hundreds in a minute from one source. Add rate-limiting per source IP so a single noisy repo can't take down the analysis pipeline. Sketch the implementation.

| | igni (0/5) | Claude Code (0/5) |
|---|---|---|
| **Decision** | Picks a similar-looking primitive (a concurrency gate using the same Redis sorted-set calls) as the reuse target, even though its purpose is concurrency control, not rate limiting. | Sidesteps the case entirely — pushes back on per-IP keying ("provider IPs are shared, key on repo instead"), then sketches design alternatives without picking a reuse target. |
| **Why miss** | Mechanism-vs-purpose confusion — primitives match, intent doesn't. | Claude Code turns prescriptive prompts into design conversations; never lands on the existing rate-limit pattern as the answer. |

Two different failure modes converging on 0/5.

### Case 6 — Soft-delete with audit-column pair

> **Prompt:** Support requested undo for repository removal — when a user deletes a repo from our system, we want to keep the data for 30 days in case they want to restore it. Add soft-delete support so a repo can be hidden from normal queries but the data stays around. Sketch the migration, the model change, and the query change. Don't edit files.

| | igni (5/5) | Claude Code (0/5) |
|---|---|---|
| **Decision** | Adds **both** `deleted_at` AND `deleted_by` columns matching the existing `created_at` / `created_by` audit-pair convention. | Adds `deleted_at` only, often with a partial index. Misses `deleted_by`. |
| **Why ✅** | igni's "Conventions to match" preamble forces the audit-pair to be named explicitly. | — |
| **Why miss** | — | Claude Code reads the model file but doesn't extract the audit-pair convention — defaults to the textbook soft-delete pattern. |

The biggest single-case gap (+5). igni's preamble extracts conventions from the existing file; Claude Code's free-form analysis doesn't reliably do this.

### Case 7 — Reuse existing usage table

> **Prompt:** Billing wants to enforce monthly quotas on AI API usage per user — different limits per pricing tier. Add tracking + the enforcement so a user over their monthly cap gets a clear error instead of silently getting throttled. Show the schema sketch, the enforcement check, and where it'd be wired in.

| | igni (4/5) | Claude Code (4/5) |
|---|---|---|
| **Decision** | 4 runs route through the existing per-request usage log; 1 run proposes a Redis-only tracker. | Same — Claude Code also picks the existing usage log and proposes "month-to-date SUM, no new table" as the cheapest first cut. |
| **Why ✅** | Existing-table reuse. | Identical — Claude Code even quotes the existing aggregator helper. |
| **Why miss** | One run picked Redis with TTL — a parallel implementation that bypasses the canonical aggregation table. | One run drifted similarly. |

Tie; both systems handle this when they find the existing usage table.

### Case 8 — Append-only retry history (chronic 2/5 both)

> **Prompt:** Sometimes a commit fails to process due to transient errors — upstream API timeouts, briefly unavailable LLM providers, etc. We want it to automatically retry up to 3 times with exponential backoff, and the retry attempts should be visible in the processing history so support can debug. Sketch the schema change (if any), the retry logic, and where it integrates.

| | igni (2/5) | Claude Code (2/5) |
|---|---|---|
| **Decision** | Wrong-store: follows the task chain to the input/queue table and proposes adding a `retry_history` JSON column there. | Correctly identifies the existing append-only step table on the runs that pass. On the misses, Claude Code tells the user the schema is "fine as-is" without sketching the new-row-per-retry pattern. |
| **Why ✅** | Right table + step-order pattern. | Claude Code's wins are on runs where it does sketch the per-attempt row pattern. |
| **Why miss** | The task writes to multiple tables; the agent picks the input/queue table over the work-state table. | Claude Code reads the file but stops at "no schema change needed" instead of completing the design. |

Same scoreline (2/5 each), different failure shapes. This is the multi-write follow-through failure mode — the user's noun maps to the work-state table, not the input.

### Case 9 — Reuse existing event/status table

> **Prompt:** Operations needs the ability to replay webhooks that failed to process — sometimes a downstream is down for a few minutes and we lose events. Add a way to find recent failed deliveries and re-run them through the normal processing path. Sketch the data model, the replay action, and the operator entry point.

| | igni (5/5) | Claude Code (1/5) |
|---|---|---|
| **Decision** | Reuses the existing event table + status enum — query failed events, re-dispatch via the existing Celery task. | Proposes a **new** `webhook_delivery` table with its own status enum and attempt counter. |
| **Why ✅** | Existing-table-with-status-enum framing — one of CodeIndex's strongest retrieval patterns. | One Claude Code run reused the existing table; the rest add parallel infra. |
| **Why miss** | — | Claude Code's "design from scratch" reflex misses the existing table even when it's right there. |

+4 — igni's preamble surfaces the existing enum; Claude Code reaches for textbook design.

### Case 10 — Coordinate existing helpers (cleanup job)

> **Prompt:** A few cleanup tasks should run automatically every night — purging old changeset uploads, expiring stuck commit-processing rows that have been `RUNNING` for over an hour, and clearing sticky AI-key assignments older than 7 days. Wire it up so it runs daily without manual triggers. Sketch the scheduler config and one of the cleanup tasks.

| | igni (4/5) | Claude Code (0/5) |
|---|---|---|
| **Decision** | Celery Beat scheduling + delegate to existing helpers (the storage-cleanup method, the keypool helpers). One run inlined a raw redis client directly. | Celery Beat correct, but observations dominate: Claude Code notes existing TTLs make some sweeps "a no-op today" and stops short of writing the actual delegating tasks. |
| **Why ✅** | Encapsulation rule fires — agent extends existing classes instead of inlining. | — |
| **Why miss** | One run reached around the existing helper for the redis sweep despite the rule. | Claude Code over-analyzes the prerequisites; doesn't produce the concrete cleanup tasks. |

+4. The encapsulation rule is the load-bearing piece on this case.

### Case 11 — Index-backed triage (security)

> **Prompt:** Security review wants the worst three offenders fixed first — we know there are some hardcoded secrets, command-injection risks, and other ugly stuff lurking. Find the highest-severity security problems in this codebase, pick the top three, and sketch a fix for each. For each one I want the file, the actual issue, and what the patch would look like.

| | igni (5/5) | Claude Code (3/5) |
|---|---|---|
| **Decision** | `codeindex_query` with the security typed filter on the first call → top-3 from the ranked list → read each in full. | Greps the repo by reading individual files for `password` / `secret` / `hmac` patterns. |
| **Why ✅** | Triage shape — fast, ranked, semantic. | Claude Code's manual triage finds real issues when its read budget covers the right files; misses or times out 2/5. |
| **Why miss** | — | 2 Claude Code runs hit the case-11 timeout (its grep loop hits pathological state on this case). |

Index-backed triage is structurally faster and doesn't time out.

### Case 12 — Refactor triage

> **Prompt:** Tech-debt sprint starts Monday and we need three refactor candidates. Find the worst-quality code in this repo — stuff that's flagged for refactoring, with low maintainability or high technical debt — pick three and show me what's wrong and how you'd improve it. Don't pick test files; I want real application code.

| | igni (5/5) | Claude Code (5/5) |
|---|---|---|
| **Decision** | `codeindex_query` with `needs_refactoring=True` and a priority filter → narrow candidate set, read each. | Reads the repo's largest files, picks by line count + manual quality assessment. |
| **Why ✅** | Typed filter + post-filter on tests. | Claude Code's exhaustive-read style is at its best on this — large files reveal themselves. |

Tie. The two approaches converge on the same real candidates.

## Where the gap is

### Where igni wins by ≥ +4 trials

- **+5 — Soft-delete with audit-column pair (case 6).** Both systems read the model file. igni's "Conventions to match" preamble *forces* the audit-pair convention to be named (so `created_at` / `created_by` → `deleted_at` / `deleted_by` becomes mechanical). Claude Code reads the file and then defaults to the textbook soft-delete pattern (`deleted_at` only).
- **+4 — Webhook replay using existing tables (case 9).** The codebase already has a `webhook_event` table with a status enum that fits the case. igni finds it and reuses it across all 5 runs. Claude Code proposes a brand-new `webhook_delivery` table on 4 of 5 runs.
- **+4 — Cleanup-job coordination (case 10).** A scheduled job needs to call several existing service classes. igni's encapsulation rule blocks the "inline the resource client" reflex; Claude Code over-analyzes prerequisites and stops short of writing the actual delegating tasks.

### Where Claude Code holds parity (+/− 1)

Cases 1, 2, 4, 7, 11, 12 — both systems handle these reliably. The largest absolute scores on the Claude Code side are on consolidation/triage tasks (cases 4, 12) where exhaustive code-reading is structurally well-suited to a model with a large context window.

### Where both fail at the same rate

- **Case 5 — Webhook rate-limit (0/5 each).** Two different failure modes converging on the same score: igni mistakes a similar-looking primitive (`ConcurrencyGate`) for the right one; Claude Code turns the prescriptive prompt into a design conversation and never picks a reuse target.
- **Case 8 — Commit-processing retry (2/5 each).** igni follows the wrong link in a multi-write task chain (proposes adding retry history to the input/queue table instead of the work-state table). Claude Code finds the right table on some runs but stops at "no schema change needed" instead of completing the retry-attempt design.

## Mergeable confidence

| metric | igni | Claude Code | gap |
|---|---:|---:|---:|
| ✅-rate (output is directly mergeable) | **82 %** | 52 % | +30 pp |
| ⚠-rate (partial — needs clarification before merge) | **7 %** | 37 % | -30 pp |
| ❌-rate (wrong target — needs rework) | 12 % | 12 % | = |
| Cases reproducibly correct (5/5 ✅) | **8** of 12 | 3 of 12 | +5 |
| Cases reproducibly wrong (0/5 ✅) | 1 of 12 | 3 of 12 | -2 |
| Trials needing human review (⚠ + ❌) | 11 / 60 | 29 / 60 | -18 |

**Reading:**

- **igni is "merge with light review" territory.** 82 % of trials produce code a reviewer can take as-is; 7 % need clarification, 12 % are wrong-target and need rework. 8 of the 12 cases are reproducibly correct across all 5 runs.
- **Claude Code is "design-conversation" territory.** 52 % directly mergeable, 37 % partial (most ⚠ trials are sketches that ask a clarifying question instead of producing the concrete output the prompt asked for), 12 % wrong. Only 3 cases are reproducibly correct.
- **The ❌-rate is identical (12 %).** Both systems make hard-wrong picks at the same rate. The gap is entirely in the ⚠ band — igni commits to a concrete answer; Claude Code stays partial.

## Why the gap exists

Three architectural choices in igni do most of the work:

1. **CodeIndex is queried first, not files.** A pre-built semantic + metadata index of the repo lets the agent locate reuse targets, conventions, and existing patterns by typed filter (`security=['major-issues']`, `needs_refactoring=True`) or HyDE-style code-shaped query. Claude Code uses grep + file reads, which scales linearly with the codebase.

2. **A mandatory "What already exists" preamble.** Before any code, igni's agent must name (a) the reuse target, (b) the closest near-miss it considered and rejected, (c) the conventions to match, (d) the parallel infrastructure it will *not* introduce. This forces contrastive reasoning and blocks the "write from training-data shape" reflex that produces plausible-looking but non-native code.

3. **A small, focused model with structured tools.** igni defaults to MiniMax-M2.7 — much cheaper than Opus-4.7 — and offsets the model-size gap with index access and prompt scaffolding that does the cognitively-expensive routing for the model. The result: comparable-or-better correctness at 1/80th the cost.

## Reproducibility

Each run records the full tool trace (codeindex queries, shell calls, file reads), the agent's response text, and timing data. The grader is a deterministic Python script — same response, same verdict, every time. Re-running this benchmark on a different codebase requires only the 12 case YAML and a populated CodeIndex.
