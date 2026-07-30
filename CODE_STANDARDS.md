# Ember Code — how the code is supposed to be written

Companion to `CODE_AUDIT.md`. That doc grades what exists; this one
prescribes what to write. Concrete patterns, drawn from the A-tier
subsystems already in this repo (`core/mcp/`, `core/code_index/`,
`core/config/permission_eval.py`, `core/guardrails/`, `chat/observerBusy.ts`).
Anti-patterns drawn from the D-tier files in the audit.

Not universal software-engineering advice. This is how *this codebase*
should look — chosen because it's what the existing best modules
already do, and it's what fixes the bugs the existing worst modules
keep producing.

---

## Hard rules — non-negotiable

These have been flagged three or more times by the code owner. Treat
as invariants, not preferences.

### Rule 1: no raw dicts — always Pydantic

Any structured data with more than one field is a `pydantic.BaseModel`.

Applies to:
- function parameters and return values
- internal state (accumulators, caches, counters carrying structured info)
- wire-format payloads to `_emit`, RPC responses, session events, event-log entries
- any value that survives more than one expression

**Wire callsites** that still take `dict` on the receiving end: pass
`model.model_dump()` (or `model_dump(by_alias=True)` when field names
differ from wire names). Wrap-and-dump at the callsite, never
construct the dict literal directly.

```python
# WRONG — construction spread across a callsite
_emit({
    "type": "visualization_delta",
    "agent_path": agent_path_id,
    "spec_id": vis_spec_id,
    "json": json.dumps(ta["spec"]),
    "final": True,
})

# RIGHT — one place defines the shape, callsite dumps it
class VisualizationDeltaEvent(BaseModel):
    type: str = Field(default="visualization_delta", frozen=True)
    agent_path: str
    spec_id: str
    spec_json: str = Field(alias="json")
    final: bool = False

_emit(VisualizationDeltaEvent(
    agent_path=agent_path_id,
    spec_id=vis_spec_id,
    spec_json=json.dumps(ta["spec"]),
    final=True,
).model_dump(by_alias=True))
```

Applies retroactively: any dict literal in code you're actively
editing must become a Pydantic model in the same edit. Do NOT
opportunistically refactor dicts in unrelated code — that's noise.

### Rule 2: no inline imports — module-top only

All `import` and `from ... import` statements go at the top of the
module. Never inside a function, method, class body, or conditional
branch. Only exception: genuine circular-import breaks. Even then,
first ask whether the module boundary is wrong.

```python
# WRONG
def visualize(spec):
    import json  # ← never
    return json.dumps(spec)

# RIGHT
import json

def visualize(spec):
    return json.dumps(spec)
```

### Rule 3: no emojis in UI or code

Never use emoji as UI icons in React clients — use inline SVGs (see
the flame brand SVG in ember-server portal). Never add emoji to files
unless the user explicitly asks. Applies to comments too.

### Rule 4: never invent data

For the visualizer specifically: if the caller gives you intent
without values ("chart AAPL 2025", no numbers), do NOT fabricate
from training knowledge. Emit an `Alert` (`tone: warning`) explaining
missing data. Charts read as authoritative; made-up numbers mislead.

Generalizes: if a downstream consumer treats your output as
authoritative, and you'd have to guess to fill it, don't guess.
Surface the gap.

### Rule 5: commits use "Ignite Ember" as co-author

Not Claude. `Co-Authored-By: Ignite Ember <noreply@igniteember.sh>`.

### Rule 6: OOP over procedural — classes own data + behaviour

Data and behaviour live together on classes. If a free function takes one
object as its first argument and touches that object's attributes, it is a
METHOD in the wrong place. Move it to the class.

The `refactor-to-standards` workflow enforces this as a hard constraint;
its audit phase catalogs offenders in seven categories:

- **free-function-with-state-first-arg** — `def foo(session, x)` that
  reads `session._pending` or calls `session.foo()`. Belongs as
  `session.foo(x)`, or as a method on a coordinator class that composes
  the session.
- **module-level-mutable-state** — module-level lists / dicts / counters
  mutated by functions. Owned by a class instance instead.
- **classvar-used-as-singleton** — `Handler.token = ...` used to smuggle
  state between calls. Move to an instance field.
- **dispatch-dict-of-free-functions** — `_HANDLERS = {"a": fn_a, ...}`
  where handlers could be methods on subclasses of a base, or a
  bound-method table on an object.
- **data-and-behavior-separated** — a Pydantic model / dataclass that
  gets passed to a bag of free functions instead of having methods on
  the model itself.
- **private-attr-reach-in** — code repeatedly touching `foo._private`
  from outside `foo`'s class. Either expose a public method, or the
  reach-in is really a method that belongs on `foo`.
- **utility-module-of-related-helpers** — a file of standalone functions
  that all share an implicit subject. The subject wants to be a class.

```python
# WRONG — session-first free function, reaches into session state
def compact_if_needed(session: Session, threshold: int) -> bool:
    if session._context_tokens < threshold:
        return False
    session._history = _summarise(session._history)
    session._compact_count += 1
    return True

# RIGHT — method on the class that owns the state
class Session:
    def compact_if_needed(self, threshold: int) -> bool:
        if self._context_tokens < threshold:
            return False
        self._history = self._summarise(self._history)
        self._compact_count += 1
        return True
```

**"Sibling files are all free-functions" is NOT a defense.** If a
package is procedural end-to-end, the file you are editing is where
OOP starts. Note the drift in the PR description; do not clone the
shape.

**Exceptions — genuine pure helpers may remain free functions:**
- Takes only primitive types (no controller / session / state object
  as first arg).
- No module-level mutable state.
- Stateless leaf (e.g. `parse_frontmatter(text: str) -> FrontMatter`).
- Small enough that promotion to a class would add ceremony without
  behavioural cohesion.

These belong in a small utility module; when in doubt, ask whether the
function has an implicit subject — if two or more free functions in the
module share the same first arg, that arg is the class waiting to be
extracted.

---

## Architectural patterns

### Pattern 1: single source of truth for state — `runPhase` over ad-hoc flags

**The problem:** state scattered across independent boolean flags with
multiple setters and multiple clearers. Each flag has 3+ places that
set it and 3+ places that clear it. No one owns the invariant.
Cancel-related bugs (like the STOP button leaving "Finalizing…"
visible) are the inevitable consequence — the cancel path doesn't
know about every flag it needs to reset.

**The pattern:** ONE typed enum representing the phase, everything
else derives from it.

```typescript
// WRONG
const [proc, setProc] = useState(false);
const [finalizing, setFinalizing] = useState(false);
// ... set/cleared from run_started, streaming_done, run_completed,
// observer bus reducer, cancel path (misses finalizing), ...

// RIGHT
type RunPhase =
  | "idle"
  | "starting"
  | "streaming"
  | "finalizing"
  | "cancelled"
  | "errored"
  | "done";

const [phase, setPhase] = useState<RunPhase>("idle");

// All UI states derive:
const showSpinner = phase === "streaming" || phase === "finalizing";
const composerEnabled = phase === "idle" || phase === "done";
const label = phase === "finalizing" ? "Finalizing…" : phase === "streaming" ? "Replying…" : "";

// Cancel is a single transition:
function cancel() {
  setPhase("cancelled");
  client.cancel();
}
```

The TUI's `run_controller.py` already does this (line 164:
`_processing = False` immediately on cancel). The web's `App.tsx`
diverged — same shape of bug, same fix pattern.

Applies to server-side too: `BackendServer._processing` +
`_current_run_task` + `_finalizing`-adjacent flags should be one
Pydantic `RunPhase` field. Every derivation reads from it.

### Pattern 2: typed events over dict payloads — the code_index model

**The problem:** dict-shaped events with `type` and arbitrary other
fields make callers guess the shape. Consumers can't statically know
what fields they'll see. Any producer change silently breaks consumers.

**The pattern:** every event is a Pydantic model with a `type` field
constrained by `Literal`. Dispatch is `isinstance` or a match statement,
not `event["type"] == "..."`.

`core/code_index/delta.py` is the canonical example. One JSONL delta
file with typed ops:

```python
class CommitOp(BaseModel):
    op: Literal["commit"] = "commit"
    sha: str
    parent_sha: str | None

class UpsertItemOp(BaseModel):
    op: Literal["upsert_item"] = "upsert_item"
    id: str  # UUID5(path) — stable across commits
    type: Literal["file", "folder", "entity"]
    path: str
    # ... every field typed, no `extra_data: dict`

class DeleteItemOp(BaseModel):
    op: Literal["delete_item"] = "delete_item"
    id: str

DeltaOp = CommitOp | UpsertItemOp | DeleteItemOp | ...
```

Apply this to:
- session event log (`session_data.event_log[]`) — one Pydantic
  subclass per event type
- wire messages (`protocol/messages.py` already does this — keep going)
- orchestrate progress events (currently untyped dicts — needs
  migration to `OrchestrateEvent` union)

### Pattern 3: `Result(error=...)` over raise-and-catch for expected failures

**The problem:** functions that raise on every failure mode force
every caller to wrap in try/except. Half of them forget one path.
The other half swallow exceptions too broadly.

**The pattern:** for *expected* failure modes (network down, auth
missing, resource not found), return a `Result` model that carries
the error. Reserve exceptions for programming bugs and truly
exceptional cases.

`core/code_index/sync_manager.py` and `fetcher.py` are the pattern:

```python
class SyncResult(BaseModel):
    ok: bool
    reason: str = ""  # populated when ok=False
    changed: int = 0

async def sync(self) -> SyncResult:
    if not self._is_git_repo():
        return SyncResult(ok=False, reason="not a git repo")
    if not self._authenticated():
        return SyncResult(ok=False, reason="not authenticated")
    if not self._registered():
        return SyncResult(ok=False, reason="repo not registered")
    # ... happy path
    return SyncResult(ok=True, changed=n)
```

Callers get the result, decide whether to log or surface. No
try/except chains, no error-message strings passed around loose.

Apply this to:
- every RPC handler (currently many raise, are caught by the
  dispatcher, and returned as generic errors)
- every hook execution result
- every tool-invocation result on the BE side

### Pattern 4: composition over god-classes — the Session refactor pattern

**The problem:** `Session` in `core/session/core.py` owns 15+
sub-systems (main_team, pool, todo_store, plan_store, loop_store,
plugin_loader, event_log, permission_mode, hook_executor,
hitl_coordinator, mcp_manager, ...) as direct fields. Every feature
adds a `self.foo_store = FooStore()` in `__init__`. The class is
2760 LoC and growing.

**The pattern:** `Session` holds one composed `SessionState` model.
Sub-systems are constructor-injected. Session doesn't own their
lifecycle, it orchestrates.

```python
# WRONG — everything hangs off self
class Session:
    def __init__(self):
        self.plan_store = PlanStore()
        self.todo_store = TodoStore()
        self.event_log: list[dict] = []
        self._event_seq = 0
        self.plugin_loader = PluginLoader()
        self.hook_executor = HookExecutor()
        self.hitl_coordinator = HitlCoordinator()
        self.mcp_manager = MCPManager()
        # ... 15 more fields

# RIGHT — composed, typed
class SessionState(BaseModel):
    session_id: str
    event_log: list[SessionEvent] = Field(default_factory=list)
    event_seq: int = 0
    plan_decisions: dict[str, PlanDecision] = Field(default_factory=dict)
    permission_mode: PermissionMode = PermissionMode.default

class Session:
    def __init__(
        self,
        state: SessionState,
        stores: SessionStores,      # holds plan/todo/loop stores
        integrations: SessionIntegrations,  # holds plugin/hook/mcp/hitl
    ):
        self.state = state
        self.stores = stores
        self.integrations = integrations
```

Applies to every god-class in the D-tier list:
- `BackendServer` → split into `Session`, `RpcRouter`, `StreamMux`,
  `ToolResultDispatcher`, `HookEventFanout`, `ChatHistorySplicer`
- `App.tsx` → hooks per concern (`useRunPhase`, `useAttachments`,
  `useCommands`, `useHitl`, `useObserver`), main component
  orchestrates
- `orchestrate.py`'s `_run_agent_streaming` → `SubAgentStreamHandler`
  class with one method per event type, replacing the 400-line if/elif

### Pattern 5: content-addressed stable IDs

For anything persisted whose location/name can change, use
content-addressed IDs. `code_index` uses `UUID5(path)` — same file at
same path gets the same ID across commits, so updates replace in
place rather than creating orphans.

```python
# WRONG — auto-incremented or randomly-generated per event
class VisualizationEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    # ↑ different id each time = duplicate cards on the FE

# RIGHT — stable per spec
class VisualizationEvent(BaseModel):
    spec_id: str  # UUID5 or hash of the spec's semantic identity
```

Applies to: session event log, tool call IDs across retries, viz
cards (currently done right).

### Pattern 6: cancel is a state transition, not a special case

**The problem:** cancel handlers reach into 4 subsystems (foreground
shell, Agno per-run flag, asyncio task, sub-agent registry). Each
has its own cleanup semantics. Miss one and the run doesn't actually
stop.

**The pattern:** cancel sets `phase = "cancelled"`. Every subsystem
that cares subscribes to phase changes and cleans itself up. No
central `cancel_run()` method that has to know about every subsystem.

Applies to both FE (React `useEffect` on phase change) and BE (an
`asyncio.Event` per phase, coroutines await on transitions).

### Pattern 7: separate the wire from the domain

`core/code_index/` has this right: `schema/` holds Pydantic domain
models (typed quality fields, categorical enums, no `tags` bag).
`pg/` holds SQLAlchemy services (persistence). `delta.py` holds the
wire format (JSONL ops). `index.py` orchestrates.

The four layers don't share types. Wire message → parsed to schema
model → written via pg service. Easy to swap SQLite for Postgres
without touching wire or domain code.

Applies to: `session_data.event_log` (currently a list of dicts on
the session — should have `SessionEvent` schema + `EventLogService`
persistence + `EventLogEmitter` wire).

### Pattern 8: small modules, one responsibility

The A-tier subsystems have small files:
- `core/guardrails/*.py` — 6 files, avg ~40 LoC each. One rule per file.
- `core/mcp/*.py` — 5 files, most under 200 LoC. Transport / config / approval / client separated.
- `core/code_index/schema/*.py` — 5 files, one domain model per file.

When a file crosses ~300 LoC, that's the smell to split. When a class
crosses 15 methods or 5 instance fields, that's the smell.

D-tier files broke this at 700+ LoC and 20+ methods. Every one grew
one method at a time, never got split.

---

## Anti-patterns — the D-tier signatures

When reviewing code, watch for these. Each one predicts a future bug.

### AP1: module-level mutable state as pub/sub

`core/tools/shell.py` has 9 module-level variables, three parallel
subscriber lists (`_start_subscribers`, `_line_subscribers`,
`_completion_subscribers`) each with its own lock, and a setter-based
global (`set_process_store`). This is a god-module.

**Fix:** class holds state, constructor injects dependencies, one
`ProcessEventBus.on("start", cb)` instead of three subscribe APIs.

### AP2: nonlocal-count > 5 in a function

`_run_agent_streaming` in `orchestrate.py` has 11 nonlocals
(`current_tool`, `last_update`, `last_preview`, `content_buf`,
`vis_last_emitted_len`, `vis_last_emit_at`, `current_run_id`,
`current_session_id`, `completed_content`, `parent_top_run_id`,
`agent_completed_emitted`). Every new feature adds another.

**Fix:** extract a Pydantic state model, pass an instance through
the handlers. Each field is named on the model, not shadowed as a
closure variable.

### AP3: boolean flag with 3+ setters and 3+ clearers

`_processing`, `finalizing`, `_current_run_task`, `_event_seq`. If
your grep for `self._foo = ` returns >3 hits AND `self._foo = False`
returns >3 hits, the flag has no owner.

**Fix:** phase enum (Pattern 1). One transition = one setter.

### AP4: `if/elif` chain over event types >5 branches

`orchestrate.py`'s `_handle` dispatches on event type with 8+ branches.
`ChatItems.tsx` dispatches on `item.kind` with 15+ branches. These
are dispatch tables written by hand.

**Fix:** dictionary dispatch (`{EventType: handler_fn}`) or per-type
class. Adding a new event type = new handler, not editing a giant
switch.

### AP5: `**kwargs` in function signatures

Every public method with `**kwargs` is untyped. Any typo becomes a
silent runtime error. This is the Python equivalent of `any`.

**Fix:** Pydantic model as the params argument. Callers construct
it, type checker verifies.

### AP6: docstrings that describe workarounds instead of intent

Any comment starting with "Fix for the bug where…", "Belt and
suspenders because…", or "Workaround: …" is a red flag. The
workaround should either be fixed at the source or moved into a
named function that documents the reason once.

`orchestrate.py` has "Belt-and-suspenders" comments. They're honest
but they also mark spots where the abstraction is wrong. Every one
should become a TODO to remove.

### AP7: comment/code ratio > 30%

If more than a third of a file is comments explaining quirks, the
code is quirky. Comments should explain *why*, not *what*.
`ChatItems.tsx` has hundreds of lines of "on the live path this
handler…" comments. Real fix: name functions after the case they
handle.

---

## Reference implementations — copy these

When adding new code, model it on these files, not the D-tier ones.

### For a new event type
- **Model:** `core/code_index/delta.py` — typed op union.
- **Wire:** `protocol/messages.py` — Pydantic messages with `type: Literal[...]`.

### For a new small pure reducer
- **Model:** `clients/web/src/chat/observerBusy.ts` — testable pure function, one thing.
- **Model:** `clients/web/src/chat/visualizationStream.ts` — partial JSON reducer.

### For a new subsystem manager
- **Model:** `core/mcp/client.py` — async client, async client class, no globals.
- **Model:** `core/plugins/loader.py` — six-root discovery with priority, namespace-prefixed apply.
- **Model:** `core/code_index/sync_manager.py` — single orchestration entry, `Result(error=...)` returns, no raises for expected failures.

### For a new eval/rule
- **Model:** `core/config/permission_eval.py` — pure module, no I/O, no network, no interactive prompts. Rules parsed once, evaluation is a pure function.
- **Model:** `core/guardrails/injection.py` — regex list + `Guardrail` protocol implementation, ~50 LoC.

### For a new persistence layer
- **Model:** `core/code_index/pg/*.py` — SQLAlchemy service per domain, indexed columns for real filter queries.
- **Model:** `core/session/persistence.py` — `_upsert_session_data_key` chokepoint, atomic-replace semantics documented.

### For a new tool
- **Model:** `core/tools/edit.py` — 166 LoC, one class, one thing.
- **Model:** `core/tools/monitors.py` — read + control paths clearly separated, "agent cannot START a monitor that isn't declared" security note.

### For a new agent
- **Model:** `bundled_agents/visualizer.md` — auto-generated from a schema (`gen-visualizer-prompt.ts`), no hand-written schema docs.

---

## Checklist — adding a new feature

Before merging:

- [ ] Every structured value has a Pydantic model. Zero raw dict literals in the diff.
- [ ] Every import is at module top. Zero function-body imports.
- [ ] Every new file is single-responsibility. If it's over 300 LoC, is that justified by domain complexity (like `code_index/index.py`) or accretion?
- [ ] Every new class has ≤15 methods and ≤5 instance fields. If more, is it composed of sub-models?
- [ ] Every new function has ≤3 nonlocals in its enclosing scope. If more, extract state to a Pydantic model.
- [ ] Every new state flag has ONE setter and ONE clearer. If more, use a phase enum.
- [ ] Every expected failure mode returns `Result(error=...)`. Only bugs raise.
- [ ] Every event type is a Pydantic class in a `Literal`-tagged union. Zero `type: str` + `payload: dict[str, Any]`.
- [ ] Every new tool call has ≤3 parameters. If more, group into a Pydantic input model.
- [ ] Tests use real dependencies where feasible; if mocking, mock the ONE seam under test. Avoid `MagicMock` for entire objects.

---

## When you disagree with these rules

Talk to the code owner before deviating. These rules exist because
the user has flagged patterns multiple times, and the audit shows
which files rot fastest when the rules are ignored.

If a rule genuinely doesn't fit a case, name the trade-off out loud
in the PR description. Don't silently break the rule and hope no one
notices — that's how the D-tier files got there in the first place.
