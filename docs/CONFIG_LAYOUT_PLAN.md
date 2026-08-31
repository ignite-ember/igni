# Where configuration lives: `.igni`, the home directory, and the server

Four decisions arrived separately and turn out to be one change:

1. Rename `.ember` to `.igni`.
2. Server configuration should not be written into the project.
3. A project file of the same name beats the server's.
4. Nothing sensitive in the project directory.

(4) is what makes (2) and (3) worth doing, and (1) is the moment to do
all of it, since every path is being touched anyway. Done piecemeal they
conflict; done together they collapse into one rule:

> **`~/.igni` holds secrets and state. `<project>/.igni` holds what a
> team shares. The server's pack is a cache neither of them contains.**

This is a plan, not a schedule. Each section says what is true now, what
should be true, and what breaks on the way.

---

## Why the project directory cannot be committed today

`<project>/.ember` currently mixes three unrelated things:

| | Examples | Should be |
|---|---|---|
| **Secrets** | `config.local.yaml` — a live provider `api_key` sits at line 19 in this repo right now | `~/.igni` |
| **State** | `sessions.db`, `data.db`, `state.db` (conversation history, so code and prompts), `attachments/` (34 pasted files here), `process_logs/`, `.checksums.json` | `~/.igni` |
| **Shareable config** | `agents/`, `skills/`, `commands/`, `rules/`, `output-styles/`, `workflows/`, `hooks/`, `settings.json`, `knowledge.yaml` | committed |

`.gitignore` has `/.ember`, and **zero files are tracked** — which is
correct, because you cannot commit that directory as it stands. The
`.local.` filename convention is the existing "do not share this"
signal, and it works *only* because the whole directory is ignored. The
moment the directory is committed, one wrong ignore line publishes a
provider key.

Splitting by **directory** rather than by filename suffix removes that
failure mode: there is no path from `~/.igni` into a repo.

A fourth category is already there and shouldn't be: **20 group-synced
agents** and a `.checksums.json` tracking 32 entries. Un-ignoring the
directory today would commit the organisation's content into every
repository, giving two sources of truth for it.

---

## 1. The server's pack stops landing in the project

**What exists.** Six kinds — `agents`, `skills`, `commands`, `rules`,
`output-styles`, `workflows` — are copied out of the pack into
`<project>/.ember/` by `GroupAgentSync`. `.checksums.json` records the
content hash at sync time so the next pull can tell "unchanged locally"
from "edited here *and* changed on the server", and the second case
raises a conflict the user is asked to resolve.

The reason is written down in `markdown_commands.py:135`:

> *"No org root: a group's commands are synced into
> `<project>/.ember/commands` so a person can edit one, and reading the
> server's copy as well would let it outrank the edit."*

So today's design already delivers "local wins" — by making the
server's copy *become* the local copy. One file, and your edit to it
survives.

**What should be true.** The pack stays in
`~/.igni/group-policy/<kind>/` and is read from there. Each loader gains
the group directory as a search root placed **before** the project's, so
a project file of the same name wins on collision.

For commands this is one line: `_commands_dirs()` already returns an
ordered list documented as *"later overrides earlier"*, so inserting the
group root ahead of `project_dir / commands` is the whole change. The
other loaders already accept a `group_dir` and are called from five
places in `session/core.py` plus `tools_builder.py`.

**What this deletes.** `GroupAgentSync`, `.checksums.json`,
`EntryConflict`, `pending_all()`, `resolve_group_conflict()`, the
`.group-agent-conflicts.json` file and the "you have local changes,
accept the server's version?" prompt. All of it exists only because
group content lands in the project. With nothing landing, there are no
conflicts to reconcile.

**What this costs, and the fix.** Today the group's `reviewer.md` is a
file in your project that you can open and adjust. Afterwards, overriding
means authoring a same-named file from scratch or digging into the cache
to copy one out. That is a real regression for the commonest case —
"the org's reviewer, but stricter about migrations".

So: `/igni eject <kind> <name>` copies the cached entry into
`<project>/.igni/<kind>/` and tells you it now wins. Explicit rather than
automatic, and a fraction of the code the sync machinery costs.

### "In memory" means "not in the project", not "no disk"

The pack should keep its on-disk cache. A pure in-memory pack cannot
start a session without reaching the server: no agents on a plane, none
during an outage, none while the server is being upgraded. The objection
was to server content appearing in the repository, and
`~/.igni/group-policy/` is invisible to both the developer and to git —
which satisfies it.

ETag revalidation already exists, so the cache is cheap to keep fresh.

### Every session start revalidates

**What exists.** `refresh_if_stale` gates on `_is_stale()`, which is a
**300-second TTL** on `pack_meta.json`'s `fetched_at`. If the cache is
younger than that, hydration returns `False` before making any request.
The comment at the call site says why — *"cheap stale check first so we
don't surface noise on every CLI invocation when the cache is fresh"* —
and for CLI invocations that is right.

But a **new dialogue** is not a CLI invocation. Starting one inside the
window silently uses a pack up to five minutes stale, which is the wrong
trade: session start is exactly the moment a developer should pick up an
admin's change, and it is rare enough to afford a round trip.

**What should be true.** Session start revalidates unconditionally.

This is close to free because the conditional-request machinery already
exists: `stored_etag()` sends `If-None-Match`, and an unchanged pack
comes back as `UnchangedPack` — a 304 with no body, after which the
cache is stamped current rather than rewritten. So "check every session"
costs one small request, not a pack download.

Concretely: `refresh_if_stale` takes a `force` flag that skips the TTL
while still sending the ETag; session start passes it; the background
poller keeps the TTL so it stays quiet.

**Two constraints on it.**

*It must never block startup.* An unreachable or slow server has to fall
through to the cached pack on a short timeout. A session that will not
begin because the portal is down is a worse failure than a session
running five-minute-old agents — and the whole reason for keeping the
disk cache is that sessions work offline.

*"Session start" needs a definition.* One CLI process can host several
dialogues, and some flows construct more than one `Session`. The trigger
should be a new conversation, not a new process, or a script that opens
sessions in a loop will revalidate on every iteration.

### Hooks are already safe

Worth stating because it is the one place local-wins could have been
dangerous. Hooks merge **by event with `APPEND`**, not by name, so a
project hook is added to the list rather than replacing anything. The
organisation's `pre-pr-review` still fires no matter what a project
declares. "Local wins for agents" does not become "a project can disarm
org policy".

Ordering keeps the group last within each event list, so the group also
retains the final veto. Nothing to change here.

---

## 2. Sensitive things move to `~/.igni`

**Moves.** `config.local.yaml`'s provider credentials, `credentials.json`,
`sessions.db`, `data.db`, `state.db`, `attachments/`, `process_logs/`,
`audit.log`, `debug.log*`, `chromadb/`, `mcp-tool-state.json`.

**Stays in the project.** `agents/`, `skills/`, `commands/`, `rules/`,
`output-styles/`, `workflows/`, `hooks/`, `settings.json`,
`knowledge.yaml`.

### The awkward part: `config.yaml` has to split

It currently carries both halves of the same subject — which providers
exist (shareable: model ids, context windows, vision support) and the
keys that reach them (not shareable). The project half should reference
providers by name; the home half holds the credentials.

This is the fiddliest piece of the whole plan and the most likely to
break a working setup on upgrade, because a project config that
mentions a provider without a key must resolve that key from
`~/.igni/config.yaml` and fail *clearly* when it cannot — not silently
fall back to a different model.

The precedence ladder in `merge_plan.py` stays as it is:

```
1. Managed policy (sysadmin)      ← above CLI on purpose
2. CLI flags
3. .igni/config.local.yaml + settings.local.json   (project)
4. .igni/config.yaml + settings.json               (project)
5. ~/.igni/settings.local.json
6. ~/.igni/settings.json
7. ~/.igni/config.yaml
8. Built-in defaults
```

`config.local.yaml` keeps its tier — a project-local override is still a
legitimate thing to want — it just must not be where credentials live by
default, and the init flow should stop putting them there.

---

## 3. The rename

**Scope, measured.** 373 references across 110 source files in
ember-code, 322 more across 51 test files, 16 in ember-server. The
directory name is **not centralised**: 71 sites use the bare literal
`".ember"`.

`.ember` also names two different things — `<project>/.ember` and
`~/.ember` — so a blind replace changes both at once, and only one of
them holds live state.

**Three steps, in order:**

1. **Centralise.** One constant, and the 71 literals replaced by it.
   Safe, mechanical, and worth doing whether or not the rename happens:
   right now "the config directory" is not a greppable concept.
2. **Flip the constant** to `.igni`. Extend the existing cross-tool
   reading — which already treats `.claude` as a sibling — to read
   `.ember` as a legacy sibling, so existing projects keep working
   without being rewritten.
3. **Migrate `~/.ember` → `~/.igni` on first run.** Rename if the target
   does not exist; log what happened.

Step 3 is not optional. `~/.ember` holds `credentials.json`, `chromadb/`
and `client_state.db`: a hard rename signs everyone out and orphans
their local index, so they re-authenticate and re-index a whole
repository. The standing "drop all backward compat" rule is about *our*
deployments; this is data on other people's machines, and a one-time
directory rename costs almost nothing next to a full reindex.

**One local wrinkle:** in this checkout `ember-server/.ember` is a
**symlink** to `ember-code/.ember`, so the two share one directory. The
migration must not follow symlinks blindly and end up renaming a target
twice.

### What it turned out to be — done

Three departures from the plan above, all found by doing it.

**A fallback, not a legacy sibling.** Step 2 proposed reading `.ember`
as a sibling the way `.claude` is read. That is wrong for this: a
sibling means *both* are scanned and merged, and these two are the same
directory under two names — merging them would double every entry.
`home_config_dir` and `project_config_dir` instead return **one**
answer, preferring the new name. So the rename became tidiness rather
than a step anything depends on, which is also what makes it safe to
refuse in every ambiguous case.

**The project side needed the fallback more than home did.** Settings
load before a `Session` exists, and the project migration runs inside
`ProjectInitializer` during Session construction — so on the first run
after an upgrade there is no `.igni` to read yet. Without the fallback
the session came up on the built-in defaults: wrong model, wrong
guardrails, no warning, because a tier whose file is absent looks
exactly like a tier with nothing set.

**One reader bypassed the helpers.** `SettingsMergePlan.default` built
its tier paths as `Path.home() / CONFIG_DIR` by hand, so neither
fallback reached the settings loader — the single most consequential
place for it. Centralising the *name* (step 1) did not centralise the
*resolution*; a constant is only as good as the helpers around it being
the only way in. Pinned now by tests that fail if the hand-built form
comes back.

A fourth, procedural: the guard test against hardcoded literals spelled
`".ember"` in its own regex, so flipping the constant made it pass
vacuously. It now builds its patterns from the constants and checks both
names.

---

## Ordering

Dependencies run roughly:

1. **Centralise the directory name** (§3 step 1). Touches everything,
   changes no behaviour, makes the rest legible.
2. **Stop syncing the pack into the project** (§1) and delete the
   sync/checksum/conflict machinery. Independent of the rename, and the
   largest reduction.
3. **`/igni eject`** — needed before (2) can be called finished, or the
   regression it introduces has no answer.
4. **Revalidate on session start** — independent of everything else and
   the smallest item here, since the ETag path already exists. Worth
   doing early: it is what makes an admin's change take effect in a
   timeframe anyone would call immediate.
5. **Split `config.yaml`** (§2). The riskiest, and easier once the
   project directory holds nothing the server put there.
6. **Flip to `.igni` + home migration** (§3 steps 2–3). Last, so the
   rename lands on a layout that is already correct. **Done** — see
   "What it turned out to be" above.
7. **Un-ignore `<project>/.igni`** — the payoff, and only safe once 2
   and 5 are done. Still to do.

## Verification

Unit tests will not catch the interesting failures here. Three of them
are wiring:

- A loader whose search order is right in a fixture and wrong in a real
  session. `scripts/check_group_session.py` boots a real `Session` and
  asks what it sees; it should assert the precedence directly — a
  project file of each kind shadowing a group entry of the same name.
- A credential resolving from the wrong tier after the `config.yaml`
  split, which looks like "it used a different model" rather than an
  error.
- The home-directory migration, which by definition only runs once and
  only on a machine that already has data. Needs a fixture home with a
  populated `~/.ember`.
- Session-start revalidation, where the failure that matters is not "it
  did not check" but "it checked and the server was down and the session
  never started". Worth a test that points the client at a dead port and
  asserts the session still comes up on the cached pack.

This is the class of bug the CI item in `docs/TODO.md` exists for, and
it is a stronger argument for it than the one already written there.
