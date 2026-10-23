# Extensibility Floor

How CodeIndex, Artifacts and third-party capabilities attach to igni — and what igni promises not to break underneath them.

> **Status: specification.** Parts are built; [Milestones](#milestones) is the only statement of which. This banner used to read "nothing below is implemented yet" while that table listed eight rows as shipped — believe the table.
>
> **"Remote" always means inside the customer's own network.** igni hosts nothing. A laptop talking to an on-prem Artifacts service is still fully air-gapped.

## One concept, two packagings

An **extension** is a capability with a service behind it. It arrives one of two ways, and nothing else about it changes.

| | **Sidecar** | **Remote** |
|---|---|---|
| Arrives as | a signed image, installed and supervised by us | a registered endpoint someone else runs |
| Tools reach the agent via | MCP over stdio | MCP over SSE/HTTP |
| Infrastructure | ours to resolve and supervise (a graph DB on the laptop) | behind their URL — scaling, HA, failover are their problem |
| Installed by | an administrator, from the image | an administrator, by registering a URL |
| Licence enforced by | withholding the image | the endpoint itself |
| Blast radius | one user; single instance | whatever their service does about it |
| Example | **CodeIndex** — repo-coupled, cannot be remote | **Artifacts** — a service with a URL |

**Any backend can attach a remote extension.** A single-user local backend can register one; a shared backend serving everyone can too. CodeIndex is sidecar-only because it is bound to a checkout, not because of where the backend runs.

**A sidecar is single-user only**, and this is now checked rather than stated. `IGNI_DEPLOYMENT=shared` marks a backend as serving several principals, and an install there refuses `sidecar` and `dev` packaging naming `endpoint` as the fix. A sidecar is one supervised process, so on such a backend it is one instance shared by all of them — one `IGNI_WORKSPACE`, one resolution of each secret, and no answer to whose.

**And no extension on a shared backend knows who is calling.** `RPCRequest.caller` is an extension id, not a principal, so even the endpoint packaging that *is* allowed there cannot tell two users apart. That is the open item, recorded as G5 in `EXTENSION_READINESS.md`; it needs a principal on the wire and a decision about whether an extension learns who somebody is or only an opaque token that is stable for them.

**UI is a single-user contribution.** A remote extension attached to a local backend may contribute a surface, and the local backend serves its bundle. A shared backend serves no extension UI — the frame is hosted by the igni app next to the user, and a shared backend has no such app.

**Tools are always MCP.** We do not invent a second tool transport. What an extension needs *beyond* tools is what this document specifies.

**One extension, one manifest.** Packaging decides which fields apply — `requires` and an image for a sidecar, `endpoint` and auth for a remote one. Everything else is identical, so a capability is never described twice and cannot drift between deployments.

---

## 1. What an extension contributes beyond tools

This is the whole justification for a contract. MCP carries tools. It does not carry any of the following, and CodeIndex needs all of them.

Everything below is a key under `contributes` in the manifest, and the list is exactly what `docs/extension-manifest.schema.json` accepts there. If this table and that file disagree, the file is right and this table is a bug.

| Contribution | What it does | Why MCP can't |
|---|---|---|
| `promptFragments` | text injected at a named anchor in a system prompt, from a tool call or a bundled file | replaces the hardcoded `{{CODEINDEX_GRAPH_SCHEMA}}` placeholder. Fetched when the extension becomes `READY` and cached, because a supplier runs during prompt assembly and that is not an async context — so a fragment changes when its extension restarts, not when its data does. |
| `promptVariants` | which agents switch to a variant while this extension is `READY` | generalises the `.codeindex.md` suffix. *Known* and *live* are separate: a variant whose extension is installed but down stays known, so its agent file is recognised and skipped rather than loaded as an agent whose name happens to contain a dot. |
| `settings` | config the host renders into its own settings surface, stored per deployment and delivered through launch params | — . Stored *beside* the bundle rather than inside it: the bundle is what was signed, and a value an administrator typed is not part of what a vendor attested to. Never a secret — those are `permissions.secrets`, resolved by the deployment. |
| `commands` | slash commands the extension answers | — . Dispatched to the tool with the command's own name, the rest of the line arriving as `args`. Declaring one *is* the grant, separately from `permissions.rpc`: that list is what the extension's UI may call, and a slash command is the user invoking something igni offered them. |
| `menu` | entries in the header tools menu | — |
| `pages` | **optional** UI — a surface the extension renders in its own frame | MCP has no UI concept |
| `events` (in `permissions`) | being told when something happens, instead of polling for it | MCP carries no push to a UI. Two kinds, one envelope: **host events** from a published vocabulary, which need a grant, and an extension's **own** events under `ext.<id>.`, which do not. |
| `indicators` | a status chip in igni's own chrome, beside igni's | MCP has no UI concept. Data rather than a frame: a chip is twenty pixels and an iframe is not what twenty pixels costs. The host draws it; the extension decides what it says, because what "preparing" means for an index is not what it means for a document store. |

`menu` rather than `tools`, because `permissions.tools` already means the MCP tool names an extension may register with the agent. See §2.

**`health` is not in this table**, though it is a contribution in every sense: `READY` / `PREPARING` / `DISABLED` / `FAILED`, always with a reason and a fix, because MCP reports connectivity and not "licence expired, install the renewed key". It is runtime state the extension reports, not something a manifest declares, and listing it beside keys that *are* declared is how it ended up being written into one.

UI is a contribution some extensions have, not what makes something an extension. A headless extension is normal.

Agents, skills and hooks are **not** here: plugins and group packs already carry those. Don't build a second path.

Everything is namespaced by extension id. A collision with a base name is a validation error at install, not a surprise at runtime.

### Events

**The vocabulary is published; igni's bus is not.** "Listen to host
events" cannot mean "subscribe to our broadcast bus" — that bus is a
refactoring surface, and an extension pinned to it would mean igni
could never change it. `HostTopic` is the promise, a `StrEnum` for
the same reason `RpcMethod` is one: the value is what travels, and
an emitter naming a renamed member fails at import rather than
delivering to nobody.

| Topic | Payload | When |
|---|---|---|
| `session.switched` | `{ session_id }` | the user moved to a different session |
| `workspace.head-changed` | `{ commit_sha, branch }` | the project's git HEAD moved — the event CodeIndex exists to wait for, so it does not poll `git rev-parse` itself |
| `workspace.files-changed` | `{ paths, truncated }` | files changed, as git sees them, filtered to your `workspacePaths` and coalesced so a build is not ten thousand events. `truncated` means the list was capped — rescan rather than trust it |
| `extension.state-changed` | `{ state, reason }` | *this* extension's own health moved |

Short on purpose. A missing event costs an extension a poll; a wrong
one costs igni a shape it can never change. Additive is never
breaking, so it grows on demand.

**The grant is not the content.** `permissions.events` decides
whether you are addressed. It does not decide what you are handed,
and for the two `workspace.*` topics the second question has a
second answer: they also require `permissions.workspace`, and
`workspace.files-changed` is filtered to your `workspacePaths`
before it is sent. An extension granted `docs/**` that was handed
the unfiltered list would have just been told every path in the
repository — with the grant check passing, so nothing would have
looked wrong. An extension with `workspace: none` hears neither
topic however they were granted: a branch name is something the
repository knows about itself.

A manifest that asks for a `workspace.*` event without the workspace
is **warned about at install, not refused** — a grant that reads like
a subscription and behaves like silence is the worst shape a
permission can have, and the author's only other evidence would be
an event that never arrives.

**What the two workspace events can and cannot see.** They are built
on `git`, polled: `git rev-parse` at 1 Hz and `git status` at half
that, both skipped entirely while no installed extension is entitled
to the result. So an ignored file is invisible, and a project that is
not a repository produces no workspace events at all. The alternative
was a filesystem watcher that would have had to learn `.gitignore`
before it was usable, and would have reported every write inside
`node_modules` until it did.

**A first observation is not a change.** The watcher learns where
HEAD is without announcing it, and forgets again when the last
listener goes away — otherwise a freshly installed extension is told
HEAD "moved" to where it has been since before it existed.

**Events reach frames, not services.** Delivery is push → client →
the one iframe it was addressed to, so an extension that contributes
no page receives nothing. A service learns about the host by being
called, which is the direction MCP runs in.

**Subscription is a grant, not a filter.** The check happens where
an event is dispatched, never where it is received — anything that
posted to every frame and asked each to discard what was not its own
would be trusting the frame to discard it, and the frame is the
untrusted party.

**An extension never hears another.** Not by `ext.*`, not by naming
one exactly: another extension's topics are unreachable rather than
merely ungranted. Its own, under `ext.<id>.`, need no grant — it is
always allowed to talk to itself. And events go host→extension only;
an extension cannot emit into another.

### Not in v1

Keybindings, context menus, custom editors, theme overrides, i18n.

Also **`chatRenderers`** (inline rendering of an extension's tool results in the chat transcript). It was in the table above before anything implemented or specified it, which made it look declarable, and it is unlikely ever to arrive: a tool result that wants a rich view emits a `json-render` spec, which the chat already renders, and a second mechanism for the same job would be a second thing to keep in step.

`indicators` was here too, for the same reason, and has moved back into the table — because the *first* extension needs one. `CodeIndexIndicator` sits in the composer today, hardcoded, and when CodeIndex becomes an extension that chip either comes from its manifest or is lost. "Nothing has specified it" was right about the specification and wrong about the need, which is the difference a real extraction surfaces and a reasoned list does not.

Additive changes are never breaking, so omitting is cheap and premature inclusion is forever.

---

## 2. The manifest

```jsonc
{
  "$schema": "https://igni.sh/schema/extension-manifest-1.json",

  "id": "igni.codeindex",             // reverse-DNS, immutable, namespaces everything
  "version": "1.4.0",
  "contract": ">=1.0 <2",             // the only compatibility axis

  "name": "CodeIndex",                // what an admin sees in an install list
  "vendor": "Ignite Ember",           // shown beside the trust tier

  "signature": "…",                   // detached; verified against a key this deployment trusts
  "trust": "first-party",             // first-party | partner | unsigned (dev only)

  // ── one of these two, by packaging. Never both, never neither ──
  "sidecar": {
    "image": "igni__code-index:1.4.0", // in the bundle; never pulled at install
    "mcp": { "transport": "stdio" },
    "requires": [                     // infra WE resolve, because there is no vendor service
      { "capability": "graph-db", "constraint": ">=5", "mode": "shared" }
    ]
  },
  "endpoint": {                       // the remote form
    "url": "https://artifacts.internal/mcp",
    "auth": { "kind": "bearer", "secret": "artifacts-token" }
  },

  "permissions": {
    "workspace": "read",
    "egress": [],
    "secrets": [],
    "tools": ["igni.codeindex.search"], // MCP tools it may register with the agent
    "rpc": ["ext.igni.codeindex.*"]     // backend methods its own UI may call
  },

  "contributes": { /* §1 */ }
}
```

**`docs/extension-manifest.schema.json` is the authority**, not this
excerpt. Point an editor at it with the `$schema` line above and it
completes and checks as you type;
`tests/test_a_manifest_is_checked_against_its_schema.py` validates
every example manifest in the repository against it on every run. That
test exists because the first manifest written against this section
broke it in three places — a sidecar with no `image`, a `menu` entry
misfiled as a `command`, and a prompt fragment with an anchor and no
text — none of which prose alone had caught.

**`tools` and `rpc` are different lists.** `permissions.tools` is MCP
tool names the extension may register with the *agent*.
`permissions.rpc` is backend methods its own *UI* may call over the
bridge. They are granted by the same administrator and enforced in
different places, and an extension commonly wants one and not the
other: a headless extension has no `rpc`, and a UI-only extension has
no `tools`. For the same reason the header-menu contributions in §1
are `contributes.menu` and not `contributes.tools`.

**Installed once.** One `id`, one install. No second copy, no per-project instance. Everything the extension needs to do, it does — two indexes is configuration inside the extension, not two installs.

**One version axis.** An extension declares a `contract` range and never names a product version. The deployment advertises one contract version, the lower of what the backend and the installed client implement.

### Requirements: what an extension needs the deployment to provide

**igni is the broker, not the provider.** A deployment running Qdrant
has a vector store igni knows nothing about and does not own; igni's
job is to hand an extension a connection to it. The one case where
igni *is* also the provider is a laptop, where the deployment is the
laptop and igni is the only thing around to act for it — which is
exactly why there are two resolvers and not one.

**Why broker it at all**, rather than let the extension connect to
Qdrant itself with its own configuration:

- **The endpoint and the credential stay out of the bundle.** An
  extension that configures its own store is an extension trusted
  with infrastructure access, and `egress` and `secrets` stop
  meaning anything — the deployment can no longer see or control
  what it reaches.
- **One bundle, every deployment.** Self-configuration means a
  different bundle per environment, or a bundle that has to know
  where it is running. Neither is a contract.
- **The operator declares once.** Ten extensions needing a vector
  store configure it once, in the deployment, rather than ten times
  in ten manifests that can disagree.

**Declare a kind, never a product.** `graph-db`, `vector-store`,
`relational-db`, `object-store`, `embedder`. A regulated customer
often has a sanctioned Postgres you must use, and a manifest naming
`postgres` cannot express "or whatever they have".

**A resolver returns a connection, never a process.** That is the
whole contract, and everything below follows from it. Two resolvers
exist and they differ only in what they do *before* returning:

| | On a laptop | In a deployment |
|---|---|---|
| Resolver | `ProvisioningResolver` | `BindingResolver` |
| What it does | downloads, starts, loads — then returns a connection | returns a connection to infrastructure the deployment already runs |
| What it builds | whatever is missing | **nothing** |
| Missing instance | provisions one | **refuses the install**, because that is an operator's problem and a backend starting one quietly is the surprise |

**The extension cannot tell which it got**, and that is the point:
the same bundle, the same signature and the same manifest install in
both places. A manifest that had to know where it was running would
not be a contract.

**Tenancy is in the result from the start.** A shared instance says
which slice is this extension's — a database name, a collection
prefix. On a laptop that is trivially "the only one" and it is
*still* filled in, because a field that appeared only in the
deployment case would make the deployment case a contract change
rather than a resolver swap.

**`mode` is the extension's requirement; the resolver decides how.**
`shared` takes the deployment's instance, `dedicated` wants one
nobody else is on, and `bind-only` means an existing instance and no
new one. Each resolver refuses the one it cannot honour: a binding
resolver refuses `dedicated` rather than quietly handing over a
shared instance — the extension asked for isolation — and a
provisioning resolver refuses `bind-only`, since starting an
instance is exactly what that mode forbids.

**`constraint` does not always mean a version.** Its meaning is
defined by the kind, and saying "a semver range" was wrong the day
embedders arrived:

| Kind | `constraint` | Resolved `uri` |
|---|---|---|
| `graph-db` | semver range, e.g. `>=5` | `bolt://host:7687` |
| `relational-db` | semver range, e.g. `>=14` | `postgresql://host:5432` |
| `object-store` | semver range | `s3://…` or a base URL |
| `vector-store` | **`dim=<n>`** — the vector width | product-dependent |
| `embedder` | **`dim=<n>`** — the vector width | `http://host/embed` |

The two vector kinds take a width rather than a version because
width is the axis on which they are compatible. A newer model of the
same width is a safe substitution; an older one of a different width
is not, and a version range cannot say that.

**An extension declaring both must give them the same width.**
Vectors written at one width and queried at another do not fail —
they return nonsense, slowly, which is the worst failure available,
so it is refused at install.

**One instance may satisfy several kinds.** `graph-db` and
`vector-store` are usually the same Neo4j: it holds the graph *and*
a native vector index on it, which is how both of igni's own
capabilities use it. That is a resolver's business, not a manifest's
— it maps each kind to a connection and two kinds may map to the
same one.

Merging them into a single kind would be the wrong fix. An extension
that wants semantic search over its own documents and no graph would
have to declare a graph database it never opens, and a deployment
running Qdrant beside Neo4j could not express that either. **A kind
is what the extension needs; an instance is what satisfies it.**

**Resolution happens at install.** An extension that starts and then
discovers it has no database fails in front of a user instead of in
front of the administrator who installed it. Unsatisfiable
requirements fail there, naming the fix.

**A sidecar is a single instance, and that is the trade.** If it dies, one user is blocked. That is the correct blast radius for a laptop, and the reason none of a remote service's HA machinery belongs here.

**A remote extension is registered, not installed.** Its availability, scaling and failover are its own. We check that the endpoint answers, that the contract range matches, and that this principal's group is entitled to it.

---

## 3. The deployment profile

Packaging is one axis; how many people a backend serves is another. A **single-user backend** and a **shared backend** differ in what igni's own state sits on, and a **profile** names that once rather than letting each store decide for itself.

| | Single-user backend | Shared backend |
|---|---|---|
| Session / scheduler / loop / memory state | SQLite (`state.db`) | **Postgres** |
| Migrations | alembic, per process | alembic, one migrator |
| Concurrency | one process, WAL | connection pool |
| Extensions | sidecar and remote | **remote only** — a sidecar is one process on one machine, so on a shared backend it is one instance shared by every principal on it |

Most of it is already portable: every SQLAlchemy-backed store — scheduler, loop, agno sessions and memory, background processes, code-index models — goes through one URL builder and alembic.

Two things have to change before a profile means anything.

**The URL builder is SQLite-only.** `core/db/urls.py` takes a *path* and formats `sqlite:///` / `sqlite+aiosqlite:///`. That is the single choke point: it becomes "profile → URL" instead of "path → SQLite URL". Small, and everything downstream follows.

**Four stores bypass SQLAlchemy entirely** — raw `sqlite3`, with WAL and `busy_timeout` set by hand in `core/session/_sqlite_utils.py`:

- `session_preferences.py`
- `session_directories.py`
- `client_state.py`
- `pending_messages.py`

These are the real work. They need porting to the same seam, or an explicit per-profile backend. Note that `session_directories` and `client_state` are precisely the stores a shared multi-user backend leans on hardest.

Two problems Postgres removes for free, both found in the multi-process audit:

- **Concurrent `alembic upgrade head`.** Guarded today by a `threading.Lock` and an in-process set — useless across processes, so two backends starting together can both migrate one file.
- **`database is locked` under load.** The SQLAlchemy engines set no WAL and no `busy_timeout`; they inherit WAL only by luck, when one of the four raw stores happens to touch the file first.

A profile is also where the air-gap answer lives: Postgres is a customer-run instance inside their network, declared like any other infrastructure, never anything we host.

---

## 4. Groups decide who gets what

Entitlement and configuration are group-level in both deployments; capability existence is deployment-level.

| Layer | Decides | User override? |
|---|---|---|
| **Deployment** | which images are installed / which endpoints exist, trust store, contract version | no |
| **Group** | entitlement, configuration, permission narrowing, which contributions appear, branding | **no** |
| **User** | preferences inside what the group allows | yes, within bounds |

- **A group may narrow, never widen.** Deployment grants are a ceiling; a pack asking for more is a validation error.
- **Hiding is not disabling.** `contributes.hide` removes a surface while the capability still runs; `enabled: false` turns it off.
- **One pack per session** — group membership resolves server-side, so there is no client-side merge to arbitrate.
- **Local deployments hold exactly one pack: the principal's.** The cache materialises plain files, including `tools/*.py` and executable `scripts/*.sh`, so a pack a user is not entitled to is a disclosure. The server decides what a deployment may hold; a laptop asking for another group's pack gets a refusal.
- **On a shared BE the policy cache must be keyed by `group_id`.** It is not today — one tree per data dir, pruned against whichever pack polled last.
- **Record `group_id` and the pack ETag in every session trace.** Different groups legitimately produce different work; without this, "why did it behave differently?" is unanswerable.

Entitlement can change mid-session (packs poll on a short TTL). A revocation moves the capability to `DISABLED` with a policy reason — not `FAILED` — closes open surfaces with that reason shown, and cancels in-flight calls.

---

## 5. The UI surface

Optional, and packaging-independent *within a single-user backend*: the client is the same whether the capability behind it is a sidecar on this machine or an endpoint elsewhere, and the local backend serves the bundle either way — from the sidecar, or proxied from the registered endpoint. A shared backend contributes no UI at all (see above). A headless extension contributes none of this.

- **Served on a loopback port of its own**, one listener per extension, by the backend the client is talking to. Not `/ext/<id>/` under one port, which is what this said first: a path prefix is a naming convention the browser has never heard of, so every extension would share `localStorage`, `IndexedDB` and the right to reach into the next one's frame. A port is part of an origin; a path is not. Entitlement and the contract check are enforced before the listener starts rather than before each byte.
- **Framed** with `sandbox="allow-scripts allow-forms allow-same-origin"`. The frame gets a real origin — its own port — and `allow-same-origin` is safe here precisely because that origin is not the host's: the frame cannot reach the embedder's document, storage or cookies, and the familiar "allow-scripts plus allow-same-origin lets a frame drop its own sandbox" applies to a frame that is same-origin *with its embedder*, which this never is.
- **Per-extension CSP**: `default-src 'self'`, and `connect-src` widened only by declared egress. The bundle cannot open an undeclared socket, which is what makes egress auditable. The bridge is `postMessage` and is not governed by CSP at all, so "no `connect-src` beyond the bridge" — the earlier wording — meant `'none'` and would have blocked the extension from loading its own data files.

> **Why `allow-same-origin` is present**, measured in Chromium against two real origins rather than argued from the spec. Without it the frame has an opaque origin and **its own `panel.js` does not load** — for a module script the blocker is CORS rather than CSP, since from an opaque origin fetching its own file is a cross-origin request, and `'self'` would refuse the rest. The frame renders and nothing in it runs.
>
> The second reason is the one that decides the bridge. A `postMessage` from an opaque origin arrives at the host as `origin: "null"`, so the host cannot tell one extension from another, and the origin check every inbound message depends on has nothing to check. With a port each, `e.origin` is that extension's own `http://127.0.0.1:<port>`.
>
> These two bullets used to specify an opaque origin *and* `default-src 'self'`. They were coherent only while every extension shared one port, where an opaque origin was the only isolation on offer.
- **Bridge API, as built**: `rpc`, `navigate`, `close`, `params`/`onParams`, `setLocation`, `theme`/`onTheme`, `tokens`. Origin, source window and per-session nonce checked on every inbound message.
- **Bridge API, not built**: `subscribe`, `identity`, `setTitle`, `setStatus`, `notify`, `storage`, and per-call budgets or cancellation. This list used to be written in the present tense alongside the one above, which told an author that live push was solved. It is not: `HostMessage` is `hello | theme | params | result`, so the only way to follow a long-running job is still to poll an RPC.

  `notify` means a message in *igni's* chrome. An extension's own toasts, inside its own frame, are published as `ToastHost`/`useToast` — the two are not interchangeable, and only something the user must see after navigating away belongs to the host.

  `storage` stays missing, but it is no longer the only way to keep a place. Each extension gets an ephemeral port, a port is part of an origin, so `localStorage` is wiped whenever the backend restarts. `setLocation` is the supported way to be restorable: the guest says where it is, the host folds that into its own URL and replays it in the next `hello`. Strings only, and small — it ends up in an address bar.

**A capability UI never holds a credential and never opens a socket.** Every privileged call goes through the backend, where credentials already live.

**The dividing rule:** the iframe owns pages; json-render owns anything inline in the chat stream. Per-message iframes wreck scrolling, selection and performance.

Per business-ops **D16**, capability UI renders in the igni app only — never inside the VS Code or JetBrains host.

---

## 6. Security

The threat model is not a hostile stranger on a marketplace. It is **a trusted author's capability, built under deadline, inside a regulated estate, driving an agent that executes what it is told.**

### Trust

| Tier | Signed by | Trusted because | Installed by |
|---|---|---|---|
| first-party | us | our key is compiled into the base | ships with the platform |
| partner | the SI, or the customer for their own code | the admin added that key to the trust store | the admin |
| unsigned | nobody | — | `--dev` only, persistent banner, never production |

**We do not sign, review or countersign anyone else's capability.** The base verifies it was signed by a key *this deployment* trusts — nothing more. Verification is local against a detached signature, no network, so it survives the air-gap.

**Only an administrator installs or registers**, and only an administrator adds a trusted key. No user install, no marketplace fetch, no install-on-demand: software inventory is an accreditation control.

### Permissions

Declared, admin-granted, base-enforced, deny by default: `workspace` (none/read/write, path-scoped), `egress` (explicit hosts, empty by default), `secrets` (named grants the deployment resolves), `tools` (MCP tool names it may register with the agent), `rpc` (backend methods its UI may call over the bridge — exact names, or a `*`-suffixed prefix, because this list is read by whoever approves the install).

**Where each one is enforced, and how far.** This list was written before any of it was, and for a while the only enforced permission was `rpc` — which made the rest a list of restrictions that restricted nothing.

| Permission | Enforced at | How far it goes |
|---|---|---|
| `rpc` | `ExtensionRegistry.authorise`, on every call | Complete. |
| `tools` | tool registration, which is igni's | Complete. A service may expose what it likes; what reaches the model is this list. |
| `egress` | the page's `connect-src`, written from the manifest; and the endpoint host igni itself dials | **The UI, completely. Not the service's own process** — confining that needs an OS sandbox igni does not apply. A sidecar can still open its own sockets. |
| `workspace` | what igni *tells* a service, as `IGNI_WORKSPACE*` | An extension that was never granted the workspace is not told where it is. A service that goes looking anyway is not stopped — same OS-sandbox gap. |
| `secrets` | resolution: an undeclared name is never looked up, and the resolver is the deployment's | The value arrives in the service's environment as `IGNI_SECRET_<NAME>`. **Not a handle** — see below. |
| `signature` | install, offline, against keys an administrator added | Complete for Ed25519. The digest covers every file and the manifest with its own signature removed, so widening a permission after signing breaks it. |

**Secrets are values, not handles.** This section said "a handle, never a value" for as long as nothing implemented it. A handle needs an exchange step, and an extension's service is an MCP server igni *calls* — there is no channel for it to call back on. So the honest statement is the one above, and the handle exchange joins the list of things not built. What that costs: a secret is in the service's process environment, visible to anything that can read it there.

**The two gaps worth naming to a customer** are the same one twice: `egress` and `workspace` are enforced where igni decides, not where the extension acts. Closing them means running a sidecar under an OS sandbox — a network namespace and a mount namespace — which is the same work as container supervision and is not started.

A capability that asks for more on upgrade **does not get it silently**; it stays at its old grants until an admin accepts. Permission drift is how extension ecosystems rot.

### The agent is an attack surface

Tool descriptions, prompt fragments and tool results all land in a model's context, so a careless capability can steer the agent rather than merely misbehave.

- **Supplied text is data, never instructions** — delimited and origin-labelled on the way in, like any untrusted file content.
- **No modifying the system prompt**, no altering another capability's tool definitions, no contributing to agents it does not own.
- **Fragments are length-capped and token-budgeted**; over-cap truncates and reports rather than crowding out the user.
- **Tools obey the permission pipeline and the hooks chain.** `BYPASS_PERMISSIONS` is unreachable from a capability.
- **Guardrails apply** on the same path as everything else.
- A result that *looks* like an instruction is still a result. Stated so a reviewer can test it.

### Egress

The air-gap claim is defended by a test (`tests/test_nothing_phones_home.py`). The contract extends it: every host declared, empty by default, UI egress structurally zero, and each capability service carries its own equivalent of that test. Undeclared egress is a contract violation, not a bug — it is the one failure that costs a customer their accreditation.

### Audit

Install, registration, upgrade, permission grant, tool invocation and bridge call — all auditable with capability id, version, principal, app identity and `group_id`.

---

## 7. Failure is normal

| If this fails | Then | Never |
|---|---|---|
| sidecar / endpoint down | `FAILED` + reason + fix; agent told it is unavailable | session crash, agent hang |
| call times out | deadline fires, cancelled, degraded once per session | silent retry storm |
| UI throws | the frame survives; `ErrorBoundary` keeps the rest of the extension up | blank page, broken shell |
| manifest / entry invalid | refused at install, every error named at once | partial load |
| contract out of range | refused, naming the version and which half is behind | "works mostly" |
| licence expired | `DISABLED` with the renewal fix | hard stop on a *paying* customer mid-contract |
| id collides with a base name | install error naming both | last-writer-wins |

Plus: validate at install not at use; supervise with backoff and open a circuit after repeated failure; atomic reversible upgrades; unknown fields ignored in both directions with a report of what was dropped.

Every call into a capability carries a **deadline and a cancellation token** — the agent loop is never blocked — and a concurrency, result-size and token budget.

---

## 8. The floor itself

- **The contract is versioned independently of the product**, semver, declared as a range.
- **Breaking** = removing or renaming a contribution, field or bridge call; changing its meaning; tightening validation on something previously accepted; changing isolation so existing UI stops working.
- **Deprecation window: two minor releases or six months, whichever is longer** — set for a customer who upgrades twice a year after a change-advisory board.
- **Additive changes are never breaking.**
- **A mismatch fails loudly and legibly**, naming the version needed.
- **Installed capabilities and versions appear in the diagnostic bundle**, so L2 escalation starts from versions, not customer content.

### The UI half of the floor

Three published things, and nothing else about how igni is drawn:

| | Where it is declared |
|---|---|
| **Design tokens** | `PUBLISHED_TOKENS` in `clients/web/src/extensions/tokens.ts` — colours, the spacing scale, the type scale, shape. Pushed live over the bridge, so an extension follows dark mode, light mode and a group's branding without asking. |
| **CSS classes** | The classes defined in `clients/ui/igni-ui.css` — `btn`, `chip`, `ui-card`, `ui-row` and the rest. Named deliberately the same as igni's own, so a change lands in both. |
| **React components** | The exports of `@igni/ui-react`. What the first two extensions actually import, plus the primitives any extension would need. |

These three are a promise. **Everything else about igni's appearance is not**, and the distinction is what keeps restyling the app possible: `theme.css` holds about two thousand classes and three dozen of them are published.

This replaces a line that read "the React component tree, CSS class names, any token not published as a theme variable" — written before there was a package, when no class name was published and the sentence was true. It stopped being true the moment `igni-ui.css` shipped, and a floor that contradicts what we hand people is worse than one that promises too much.

## Not in the floor

- Importing anything from `ember_code.*` — the contributions are the interface, not the package.
- **Any CSS class not defined in `igni-ui.css`**, any component not exported from `@igni/ui-react`, any token not in `PUBLISHED_TOKENS`. Naming a class is not the same as publishing it: an extension that reaches for `.kb-row` because it saw it in the DOM has taken a dependency we never offered.
- The markup those classes are applied to — which element, which nesting. The class is the contract; the tree under it is ours.
- `RpcMethod` members not namespaced to a capability.
- Database schemas, Neo4j labels, on-disk layouts.
- Base prompt text and base agent definitions.
- **Anything not named in this document.**

## 9. Developing one

- **The reference extension is `examples/reference-extension/`** — a compliance scanner, around 200 lines, written against the published surface and nothing else. It is the answer to "what does an extension look like", and a standing check on this contract: anything it has to hand-roll is something the floor is missing, and its README keeps that list.
- **`igni ext init <dir>`** scaffolds one that runs as written — manifest, an MCP server, a framed page with the bridge inlined so there is no build step, and a README. `igni ext doctor` checks a bundle and prints the contract version this igni implements; `igni ext install` copies it into `<data>/extensions/<id>/`, prints the permissions it is being granted, and the backend starts it at its next boot. `list` and `remove` do what they say.
- **An install outlives the process that made it**, which is why the CLI writes to a store rather than talking to a running backend: a CLI that needed one could install nothing before it started, and nothing would survive it stopping.
- **Dev mode** is `IGNI_DEV_EXTENSIONS=1`, the only thing that makes `trust: unsigned` installable. An environment variable and not a setting a bundle can reach, because the point of the trust tier is that the easiest way to ship is not the one with no provenance. The persistent banner and hot reload do not exist yet.
- **`igni ext doctor`** checks manifest schema, contract range, permissions, collisions, deprecations, the MCP round-trip and the phones-home scan. **If doctor passes, it loads** — that equivalence is the promise.
- **Errors carry fixes**, because the reader is often a partner's L2 engineer with no internet.
- **Observability**: logs in the Watcher panel, state in the subsystem view, versions in the diagnostic bundle.
- **A changelog for the contract itself.**

---

## Milestones

| Step | State |
|---|---|
| Subsystem status (`READY`/`PREPARING`/`DISABLED`/`FAILED` + reason + fix) | **shipped** |
| Permission pipeline + hooks chain | **shipped** |
| Guardrails (PII / injection / moderation) | **shipped** |
| MCP client, stdio + SSE | **shipped** |
| Group policy packs | **shipped** |
| json-render catalog + action round-trip | **shipped** |
| Phones-home test | **shipped** |
| Capability services as separate images | **shipped in the service repos** (`igni__code-index`, `igni__artifacts`) — and **not extracted here**: both still live in `core/` and are imported unconditionally by the agent and tool builders, so neither is separable. See `docs/EXTRACTING_A_CAPABILITY.md`. |
| Prompt fragments / variants as contributions | **shipped** (`extensions/contributions.py`, tied to `READY` rather than to being installed) |
| Manifest + validation | **shipped** (`docs/extension-manifest.schema.json` for the author's editor, `extensions/manifest.py` for the installer, held to each other by `tests/test_the_manifest_model_and_its_schema_agree.py`) |
| Manifest signature verification | not started — the field is modelled and required for any trust tier but `unsigned`; nothing checks it against a key yet |
| `ext.<id>.*` RPC namespace | **shipped** (`extensions/registry.py`, `extensions/rpc.py`, `extensions/service.py`) |
| Sidecar supervision / install pipeline | not started — the registry takes a manifest and a bound service; nothing yet starts a container or registers an endpoint |
| Backend static origin, one port per extension | **shipped** (`extensions/origin.py`) |
| Sandboxed iframe + bridge | **shipped** (`extensions/ExtensionFrame.tsx`, `bridge.ts`, `protocol.ts`) — the host-side permission check now has its backend counterpart; nothing renders the frame yet |
| Policy cache keyed by `group_id` | not started |
| Remote extension registration in group packs | not started |
| Requirement resolver (local only) | not started |
| Deployment profile: URL builder + the four raw-sqlite3 stores | not started |
| `igni ext init` / `doctor` | not started |

**First consumer is CodeIndex**, ported while still in-tree. `CodeIndexPanel` takes two props, and the bridge hosts it with one line changed — its prop type, from the concrete client class to the one method it uses.

That proves the easy half and should not be quoted as more. All four of its RPCs are base `RpcMethod` members, so what was demonstrated is a first-party panel reaching first-party methods through a postMessage hop, in a unit test, against a stub window.

### A third party calling its own service

This used to say it was not possible, and it was right: `RpcMethod` is a closed enum and the router validates against it exhaustively, so `ext.acme.scanner.list` missed the dispatch table and came back `Unknown RPC method` — a message indistinguishable from a typo, which is why the gap survived being written down in three places.

The namespace is now real.

**Grammar.** `ext.<extension-id>.<method>`. The method is the final segment and the id is everything before it, so `ext.a.b.c` is id `a.b` and method `c`. Resolving that against the installed set instead would have meant a call's meaning changing depending on what else happened to be installed.

**Not an enum member.** `RpcMethod` stays closed, because closing it is what gives the base its "every method is registered or boot fails" guarantee. Extension methods arrive with a manifest rather than at import time, so they get a prefix checked by a registry. Anything under `ext.` is answered there, including a malformed name — falling back would reproduce the `Unknown RPC method` that hid this.

**Five checks, in this order**, each raising with a sentence that names the fix:

1. the name parses as an extension method;
2. the request says which extension it came from — `RPCRequest.caller`, set by `IgniClient.asExtension`. igni's own UI has no business in this namespace, and a client too old to identify itself cannot be trusted to be either;
3. the caller *is* the extension it is addressing. The id is in the method name, so without this, spelling somebody else's namespace would be enough to read their service;
4. that extension is installed and `READY`, and a refusal repeats the state's reason — "licence expired, install the renewed key" is not something MCP can report;
5. the method is in the manifest's `permissions.rpc`.

Authorisation is decided before the service is consulted, and that ordering is tested rather than asserted: otherwise a permissions problem surfaces as a connection error and sends an author looking in the wrong place.

**Enforced here, not in the client.** The web client checks the grant before forwarding and its comment said the backend re-checked. That was aspirational — there was nothing to re-check with. A frame is a renderer; its check is a courtesy that makes the error immediate.

**The transport is MCP**, per §Principles. An extension's service answers `ext.<id>.<method>` as a tool call named `<method>`, on the same server that serves the agent. Two grant lists over one server: `permissions.tools` is what the *agent* may call, `permissions.rpc` what the extension's own *UI* may call. `structuredContent` is used where a server sends it; otherwise text blocks are joined and parsed as JSON when they are JSON, because that is what a server written for a UI sends.

**What is still missing** is the install pipeline. `ExtensionRegistry.install` takes a validated manifest and `ExtensionRpc.bind` takes a service; nothing yet starts a sidecar container, registers an endpoint, or verifies a signature. So the namespace works and has no production path to being populated — which is the next thing, not this thing.

## Reference

- Closed unions to open: [`clients/web/src/chat/pageStack.ts`](../clients/web/src/chat/pageStack.ts), [`clients/web/src/App.tsx`](../clients/web/src/App.tsx)
- Subsystem contract: [`backend/subsystem_status.py`](../src/ember_code/backend/subsystem_status.py)
- Permissions: [`core/config/permission_eval/`](../src/ember_code/core/config/permission_eval), [`core/config/tool_permissions/`](../src/ember_code/core/config/tool_permissions)
- Group tier: [`core/config/group_policy.py`](../src/ember_code/core/config/group_policy.py)
- Store seams: [`core/db/urls.py`](../src/ember_code/core/db/urls.py), [`core/db/engine_registry.py`](../src/ember_code/core/db/engine_registry.py), [`core/session/_sqlite_utils.py`](../src/ember_code/core/session/_sqlite_utils.py)
- MCP: [`core/mcp/config.py`](../src/ember_code/core/mcp/config.py), [`docs/MCP.md`](MCP.md)
- Prompt placeholder to generalise: [`core/prompts/__init__.py`](../src/ember_code/core/prompts/__init__.py), [`core/agents/loader.py`](../src/ember_code/core/agents/loader.py)
- Air-gap enforcement: [`tests/test_nothing_phones_home.py`](../tests/test_nothing_phones_home.py)
- Commercial framing: `igni__business-ops/strategy/decisions.md` (D1, D3, D4, D7, D11, D16), `strategy/license-enforcement.md`
