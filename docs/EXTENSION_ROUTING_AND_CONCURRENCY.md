# Extension routing and concurrency

## Status

Agreed design (2026-10-25). **Built for one person** (§11): extensions
enabled per group, the person's groups in their session and in the
extension listing, a machine installing only what is on for their
groups, every call naming its principal, `Caller` and
`ext.service.for_(caller)` in the SDK, and the concurrency harness.

**Superseded — §3 apps, §5–§8 shared-backend routing:** the cloud/API
mode is being redesigned. The likely shape is one container per producer
(a person or an app), each running a single-person igni backend with a
persistent volume, behind a gateway that authenticates the producer and
wakes and routes to its container; idle containers are stopped, and heavy
models are shared per node. Every backend then serves exactly one
producer, so per-principal filtering inside one backend, a shared
backend's sidecars and per-principal token exchange are not built and may
not be needed. Those sections stay below as the record of the earlier
design; the cloud design under discussion replaces them.

**Current** — what is built (§11) and holds under either design:
per-group enablement (off by default), the person's groups in their
session and in the extension listing, a machine installing only what is
on for their groups, every call naming its principal, `Caller` and
`ext.service.for_(caller)` in the SDK, and the concurrency harness.

## 1. The rule this serves

An extension's backend logic runs in every mode; its UI loads only where
there is an app to host it.

| Mode | Who calls | Extension process | Service | UI |
|---|---|---|---|---|
| App (desktop / web) | one person | runs | called | loaded into the host |
| Headless (`-m`, `--pipe`) | one person's script / CI | runs (today: not started) | called | none |
| API / SDK (`IGNI_DEPLOYMENT=shared`) | many people and apps through one backend | runs, shared by all callers | called | none |

Two requirements follow:

1. **Routing.** An extension is enabled for chosen groups (and apps). A
   call from app1 must only reach what app1's groups enabled, and the
   extension must act for app1, not for app2 or for whoever happened to
   call last.
2. **Parallelism.** One extension process serves every caller of its
   backend at once. It must never let one slow call stall the others.

The extension does **not** do authorisation. The floor only routes a
call to an extension when the caller is allowed to make it; the
extension trusts the caller identity the floor hands it, and the
service trusts the token. The extension's one job here is to serve in
parallel and keep callers apart.

## 2. Today

- **Distribution is all-or-nothing.** An approved service's extension
  goes to every org and every person. Per-group control left with
  group-policy push.
- **Groups exist** on igni server (`org_groups`, `org_group_entries`:
  agents / mcps / plugins / settings, a Default group every user lands
  in). Extensions are not one of the entry kinds.
- **There is no "app" identity.** Callers are people (sessions) or
  services (client credentials for themselves). Nothing represents
  "app1, a program in this org that drives igni".
- **Calls carry no caller.** `RPCRequest.caller` is an extension id, not
  a principal (G5 in `EXTENSION_READINESS.md`). Fine on one person's
  machine, wrong on a shared backend.
- **The shared profile allows remote extensions only** — a sidecar would
  be one process shared by everyone, which is exactly what we now want,
  once calls carry a caller.
- **Concurrency inside the process.** The MCP server runs each request
  as its own task, so `async` tools interleave on I/O. A **sync** tool
  runs on the event loop and blocks every other caller until it
  returns; CPU work (embedding, parsing) does the same even in an
  `async` tool.
- **Headless runs start no extensions**, so their tools, hooks and
  prompt contributions are absent there.

## 3. Identity: two kinds of producer

Every call into an extension carries a **principal**: the producer the
call is for. There are two kinds, and they are not connected.

```
Principal
  kind:    person | app
  id:      stable id on igni server (user id / app id)
  org:     organisation id
  groups:  [group ids]            # people only; an app has none
  session: igni session id | null # which conversation, when there is one
```

- **People** belong to groups. Groups hold people, nothing else.
- **Apps** are the other producer. An org admin registers an app on the
  portal (name, owner); it gets client credentials (private_key_jwt,
  same as services) and calls igni's API **as itself**. An app is not in
  any group and does not act on behalf of a person.
- A person's groups are resolved by igni server at token time and
  carried in the igni token; the backend never trusts groups a caller
  claims.
- In app mode the principal is always the signed-in person.

## 4. Enablement: who gets which extension

- An approved extension is **off until an org admin enables it**. It is
  enabled per **group** (for the people in it) and per **app**,
  separately — an app's enablement has nothing to do with groups.
- A group's extensions are a table of their own (`org_group_extensions`,
  igni server §8.3), not an `org_group_entries` kind: an entry is a file
  igni writes from the group pack, and an extension never travels in the
  pack. An app will carry its own list of enabled extensions.
- A super admin still approves the *service* once (trust). The org admin
  decides which groups and apps use it. Priority (who wins a collision)
  stays the super admin's single order.
- The extension list a backend receives is "every extension enabled for
  at least one group or app this backend serves", each with the groups
  and apps it is enabled for.

## 5. Routing: where the check happens

The floor checks, once, at the edge — the extension never sees a call
it should not get.

For each call the backend resolves the principal, then filters by the
principal's groups:

| What | Filter |
|---|---|
| Tools offered to the agent | only tools of extensions enabled for the principal |
| Agent tool calls, page RPCs, slash commands | refused before dispatch if the extension isn't enabled for the principal |
| Hooks (PreToolUse, UserPromptSubmit, Stop, plan submission…) | only extensions enabled for the principal run in the chain |
| Prompt overrides / edits | applied per session, from the extensions enabled for that session's principal |
| Host-model reactions (`@ext.on`) | delivered for sessions/windows whose principal has the extension |
| Notifications from the service | already per person on igni server (unchanged) |

So app1 and app2 calling the same backend at the same time get
different tool lists, different hook chains and different prompts, each
from its own enablement — and an extension enabled only for app1 (or
only for the legal group) never sees app2's calls at all.

Prompt and agent assembly becomes per principal (today: per backend).
Cache it by the *set* of enabled extensions, not per person, so a
hundred people in one group share one build.

## 6. The wire: what the extension receives

Every request from the host to the extension process carries the
principal in MCP's request `_meta`:

```json
{"_meta": {"igni/principal": {"kind": "app", "id": "…", "org": "…",
           "groups": ["…"], "session": "…"}}}
```

The SDK turns it into a typed `Caller` the tool receives:

```python
@ext.tool(hints=ToolHints(read_only=True, ...))
async def search(query: str, caller: Caller) -> Results:
    return await ext.service.for_(caller).post("/v1/search", json={...})
```

- `ext.service.for_(caller)` asks the host for a service token **for
  that principal** (the host does the token exchange with igni server;
  the extension still never sees an igni token). The service sees the
  real person or app, with its org, exactly as today.
- `ext.host.*` calls made while handling a request are scoped to that
  request's session by default; reaching another session is refused
  unless it belongs to the same principal.
- Background work (`on_start`) has no caller, and how it runs is the
  extension's business: local work in the extension's own threads or
  processes, server work in the service's own workers (Celery). The floor
  neither runs nor schedules it. Without a caller it can only use the
  service as **itself** (client credentials).

## 7. Parallelism: the extension's own business

An extension is a Python program of a specific shape, and only services
a company trusts are approved. So the floor does not provide thread
pools, process pools, copies or queues for it.

- **The floor's only promise:** it sends calls concurrently — it never
  serialises one producer behind another — each with its principal, and
  each with a timeout. A call that times out fails with a typed error.
- **The extension's responsibility:** serve those calls in parallel and
  keep callers apart. It may use threads, process pools, subprocesses,
  its own workers — whatever it needs.
- **What the docs and starter must say plainly**, because it is the
  trap: the MCP server runs each request as its own task, but a **sync**
  tool, or CPU work inside an `async` tool, runs on the event loop and
  stalls every other caller. Use `async` I/O, and move blocking or CPU
  work off the loop (`anyio.to_thread`, a `ProcessPoolExecutor`). Keep
  per-request state in the request (the `Caller`), never in globals.
- The SDK's `testing` module gets a harness that fires concurrent calls
  from different principals, so an author can see whether their
  extension blocks or leaks callers. It is a test aid, not a runtime.

The service side is already parallel (FastAPI + per-org Celery, as
documented in `igni__code-index/docs/tenancy.md`); nothing changes there.

## 8. Headless and API mode

- `-m` / `--pipe` start the extensions enabled for the signed-in person,
  wait for them to be ready (bounded), and stop them at exit.
- If an extension whose hooks are fail-closed (tool calls, messages)
  cannot start, those events are refused with the reason — a policy
  extension cannot be bypassed by running headless.
- The shared profile drops "remote only": sidecar extensions run there,
  shared by all callers, with principals on every call (section 6).

## 9. What changes where

| Repo | Change |
|---|---|
| ember-server | `App` producer + portal page with its enabled extensions; `extensions` group entry kind + per-group enablement UI (off by default); groups in igni tokens; token for a principal on a service's audience; extension list carries groups and apps |
| igni (floor) | principal resolution per request; per-principal tool/hook/prompt filtering; principal in MCP `_meta`; per-principal service tokens; concurrent dispatch with timeouts; shared profile allows sidecars |
| igni-extension SDK | `Caller`; `ext.service.for_(caller)`; concurrency test harness; docs on not blocking the loop |
| igni-service SDK | nothing new — services already see the person/app through the token |
| CodeIndex, Artifacts, starter | take `caller` in tools; move blocking work off the event loop themselves |
| Docs | `EXTENSIBILITY.md`, `service-identity.md` (principal claim, app clients, exchange), `HOST_OBJECT_MODEL.md` (session scoping), close G5 |

## 10. Decisions (2026-10-25)

1. Apps and groups are separate producers; an app acts only as itself.
2. New extensions are off until enabled for a group or an app.
3. Background work is the extension's: local in its own processes,
   server-side in the service's workers.
4. No pools, copies or queues in the floor; the extension runs its own.

## 11. Built (single person)

| Where | What |
|---|---|
| igni server | `org_group_extensions` (tenant migration `t0003`); `GET`/`PUT /v1/portal/groups/{group_id}/extensions[/{extension_id}]` (org admin); the group page's **Extensions** tab; `GET /v1/extensions` lists only what is on for the person's groups, with top-level `groups` and each extension's `groups`, and `priority` naming only those; bundle and assets a 404 otherwise; the CLI session token carries `groups` |
| igni | `DistributionListing.entitled()` installs only extensions whose `groups` meet the person's, and removes one switched off; the person's groups kept in the store (`.groups.json`); `SignedInPerson` is the principal; every tool call to an extension's server carries `_meta["igni/principal"]` (`RequestMetaSession`, only for `source == "extension"`); every hook frame carries `principal`; the token endpoint refuses `X-Igni-Principal` naming anybody but the signed-in person |
| igni-extension | `Caller` (a parameter typed `Caller` or `Caller \| None`, filled from the call and left out of the tool's input; `Caller.current()` anywhere inside it, worker threads included); hook handlers take one too; `ext.service.for_(caller)` (`X-Igni-Principal`, one token cache per caller); `testing.ConcurrencyHarness`, `HostHarness.call(..., caller=)`, `FakeHost.hook(..., caller=)` |
| CodeIndex | `status`, `indicator`, `overview`, `breakdown` take `caller: Caller \| None` and ask the service as them; repositories remembered per caller |
| Starter | every tool takes `caller: Caller` and calls `ext.service.for_(caller)` |

Not yet: the session id on the principal (the host has no per-call
session today, so `session` is `null`); per-principal filtering inside
one backend (one person per backend makes it the install itself);
Artifacts' tools taking `caller` (its extension is being rewritten).
