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

**One extension, one manifest.** Packaging decides which fields apply — an image for a sidecar, `endpoint` and auth for a remote one. Everything else is identical, so a capability is never described twice and cannot drift between deployments.

---

## 1. What an extension contributes beyond tools

This is the whole justification for a contract. MCP carries tools. It does not carry any of the following, and CodeIndex needs all of them.

Everything below is a key under `contributes` in the manifest, and the list is exactly what `docs/extension-manifest.schema.json` accepts there. If this table and that file disagree, the file is right and this table is a bug.

| Contribution | What it does | Why MCP can't |
|---|---|---|
| `prompt` | the agent's prompts: replace the whole main prompt (or plan mode), replace one named section, add to a slot igni leaves, or stand in for one of igni's agents — each optionally only while a condition the extension reports holds | MCP has no prompt. See *The agent's prompt* below. |
| `promptFragments` | text injected at a named `{{ANCHOR}}` in an agent's prompt, from a tool call or a bundled file | replaces the hardcoded `{{CODEINDEX_GRAPH_SCHEMA}}` placeholder. Fetched when the extension becomes `READY` and cached, because a supplier runs during prompt assembly and that is not an async context — so a fragment changes when its extension restarts, not when its data does. |
| `settings` | config the host renders into its own settings surface, stored per deployment and delivered through launch params | Rendered by igni's **Extensions** page (`/extensions`), a control per declared `type`. Stored *beside* the bundle rather than inside it: the bundle is what was signed, and a value an administrator typed is not part of what a vendor attested to. Never a secret — those are `permissions.secrets`, resolved by the deployment. A change applies the next time your page is opened; a frame already up keeps the values it launched with. |
| `commands` | slash commands the extension answers | — . Dispatched to the tool with the command's own name, the rest of the line arriving as `args`. Declaring one *is* the grant, separately from `permissions.rpc`: that list is what the extension's UI may call, and a slash command is the user invoking something igni offered them. |
| `menu` | entries in the header tools menu | — |
| `pages` | **optional** UI — a surface the extension renders in its own frame | MCP has no UI concept |
| `host` (in `permissions`) | reaching igni itself — sessions, the chat, the UI, plugins, models, everything igni's own UI can do — and being told everything that happens in it, instead of polling for it | MCP runs host→server and knows nothing of igni. The **host object model** (`docs/HOST_OBJECT_MODEL.md`): objects with properties, signals and methods, granted per object. See below. |
| `indicators` | a status chip in igni's own chrome, beside igni's | MCP has no UI concept. Data rather than a frame: a chip is twenty pixels and an iframe is not what twenty pixels costs. The host draws it; the extension decides what it says, because what "preparing" means for an index is not what it means for a document store. |

`menu` rather than `tools`, because `permissions.tools` already means the MCP tool names an extension may register with the agent. See §2.

**`health` is not in this table**, though it is a contribution in every sense: `READY` / `PREPARING` / `DISABLED` / `FAILED`, always with a reason and a fix, because MCP reports connectivity and not "licence expired, install the renewed key". It is runtime state the extension reports, not something a manifest declares, and listing it beside keys that *are* declared is how it ended up being written into one.

UI is a contribution some extensions have, not what makes something an extension. A headless extension is normal.

Agents, skills and hooks are **not** here: plugins and group packs already carry those. Don't build a second path.

Everything is namespaced by extension id. A collision with a base name is a validation error at install, not a surprise at runtime. What cannot be namespaced — a prompt section, a slash command, an agent of igni's, an anchor — is a *claim*, and two extensions claiming one thing is a collision a super admin decides (below).

### The agent's prompt

An enterprise that wants its agent to work its own way gets exactly that. `contributes.prompt` has three lists:

```json
"prompt": {
  "replace": [{"target": "main", "source": "file", "from": "plugin/prompts/main.md", "when": "index-covers-head"},
              {"target": "main.safety", "source": "file", "from": "plugin/prompts/safety.md"}],
  "slots":   [{"slot": "main.code_intelligence", "source": "file", "from": "plugin/prompts/about.md"}],
  "agents":  [{"agent": "explorer", "from": "plugin/stand-ins/explorer.md"}]
}
```

- **The prompts are documents with named parts.** `main` is the main agent's system prompt, `plan_mode` what it is told about plan mode. Each is marked up in igni's markdown — `<!-- section:workflow -->…<!-- /section:workflow -->` for igni's text an extension may replace, `<!-- slot:code_intelligence -->default<!-- /slot:code_intelligence -->` for a place igni leaves, whose default shows only while nothing fills it. Every one is listed in `docs/prompt-targets.json`, generated from the markdown and held to it by `tests/test_prompt_targets_are_listed.py`. Every `##` of the main prompt is inside a section.
- **`replace`** takes `main`/`plan_mode` whole or one section (`main.workflow`). The text may use igni's own slot markers, `{{AGENT_CATALOG}}` and the host's conditionals, which work as in igni's text.
- **`slots`** add text. Several extensions' fills stack in extension-id order, each under `### From <name>` when there is more than one; one fill is at most 8,000 characters (`MAX_SLOT_CHARS`) — replacing a section is how to say more. Whether a slot is filled is itself a conditional, `<!-- slot.<id>:only -->`/`:absent`, for guidance elsewhere that depends on it.
- **`agents`** stand in for one of igni's agents under igni's name, at `EXTENSION_OVERRIDE` priority above anything a person or organisation wrote, with a file the extension's plugin carries outside its `agents/` (which holds the plugin's own agents, under the extension's namespace). The file gets the plugin security envelope.
- **`when`** names a condition only the extension can judge. Its process says so on its own MCP connection — `notifications/igni/conditions` `{"conditions": {"index-covers-head": true}}`, `await ext.prompt.hold("index-covers-head", True)` in igni-extension — and igni rebuilds every session's agents, once, on the next turn of the loop (`backend/agent_refresh.py`). Nothing polls. The connection names the sender, so an extension speaks only for its own conditions; none survive a restart.

All of it follows `READY`, like fragments: an extension that goes down takes its text and its stand-ins with it. Implemented in `core/prompts/document.py` (the markup), `core/prompts/contributions.py` (the live table the prompt builder, plan mode and the agent loader read) and `extensions/contributions.py` (registering into it).

### Collisions, and a super admin deciding them

`extensions/impact.py` reads an extension's **impact** off its manifest — prompt targets replaced, slots filled, agents stood in for, anchors, commands, conditions, and for review its tools, pages, menu, indicators, settings, host and RPC grants, egress, secrets and workspace access. igni server shows it on the Services page.

**What collides.** A claim is spelled `<kind>:<name>`:

| Claim | Collides with |
|---|---|
| `prompt:main` (the whole prompt) | another `prompt:main`, and **any** `prompt:main.<section>` |
| `prompt:main.workflow` (a section) | the same section, and `prompt:main` — never a *different* section |
| `command:/review` | the same command — and never one of igni's own (below) |
| `agent:explorer` (a stand-in) | the same agent |
| `anchor:GRAPH_SCHEMA` | the same anchor |

**Never a collision:** slot fills (they stack); pages, indicators and settings (namespaced by the extension id); tools (registered under the extension's server name); a plugin's own agents (under the extension's namespace); grants, egress, secrets (they only add). A `when` does not avoid a collision: two extensions replacing one section under different conditions still both claim it. Host-model signals are observed, never intercepted — there is no veto or rewrite hook to collide over; one added later is a claim.

**igni's own commands are nobody's.** `/clear`, `/plan` and the rest of `docs/igni-commands.json` are answered by igni before any extension, so an extension declaring one would never run, and there is nothing to decide. igni server refuses such a bundle at upload; a machine running a newer igni, with a command the bundle did not know about, switches the claim off and says why (`IgniCommands`, the resolver's `reserved`). A test holds the list to the dispatcher, and igni server's copy to it.

**Deciding.** A super admin picks the winner of each collision on igni server (service-identity §8.1). A decision names the claim, the extensions and the winner, with who and when. It covers any collision among the extensions it names — all of them or some, as on a machine that has two of three — and a collision with an extension it did not name is new. The losers lose that claim only. Whole prompts are settled first: if the extension replacing `main` wins, every other extension's `main` sections are off and no section collision is left; if a section's extension wins, only the whole-prompt one loses, and the sections meet again, one by one. When a decision's winner is not among the extensions present, all of them lost.

**On igni server** a version with an undecided collision against what is distributed is held, and the previous version keeps being distributed, until it is decided. **On a machine** the decisions arrive with the distribution listing, are kept in the store (`.resolutions.json`, so a machine that cannot reach igni still applies them), and `ImpactResolver.enforce()` runs over everything installed whenever that changes — an undecided collision (a development install colliding with a distributed extension, say) is switched off for every extension in it. What an extension does not get, and why, is in the extensions panel (`switched_off` on `list_extensions`) and nowhere is it silent.

One rule, two implementations: igni server's `app/services/identity/impact.py` and this one both run `tests/fixtures/extension_impact_cases.json`, which both repositories carry and each checks against its sibling checkout.

### Events

Being told when something happens is the host object model's signals
(below): an extension subscribes with `@ext.on(ext.host.<object>.<signal>)`
from its process or `bridge.on(bridge.host.<object>.<signal>, fn)` from
its page, and every signal is granted by `permissions.host` like the rest
of its object. There is no second mechanism. The `permissions.events`
grant, the bridge's `onEvent` and a service's `igni_on_event` tool that
carried these before the model are gone, and a manifest that declares
`permissions.events` does not validate.

Four signals predate the model and keep their old topic strings as wire
names. `HostTopic` is that list, a `StrEnum` for the same reason
`RpcMethod` is one: the value is what travels, and an emitter naming a
renamed member fails at import rather than delivering to nobody.

| Topic | Payload | When |
|---|---|---|
| `session.switched` | `{ session_id, window }` | `sessions.current_changed`: a window moved to a different session |
| `workspace.head-changed` | `{ commit_sha, branch }` | `workspace.head_changed`: the project's git HEAD moved — the signal CodeIndex exists to wait for, so it does not poll `git rev-parse` itself |
| `workspace.files-changed` | `{ paths, truncated }` | `workspace.files_changed`: files changed, as git sees them, filtered to your `workspacePaths` and coalesced so a build is not ten thousand signals. `truncated` means the list was capped — rescan rather than trust it |
| `extension.state-changed` | `{ extension_id, state, reason }` | `extensions.state_changed`: an extension's health moved (needs `extensions`; `self.state_changed` is your own, always) |

**The grant is not the content.** The two workspace signals belong to
`host.workspace`, granted by `permissions.workspace`, and
`workspace.files-changed` is filtered to your `workspacePaths` before it
is sent. An extension granted `docs/**` that was handed the unfiltered
list would have just been told every path in the repository — with the
grant check passing, so nothing would have looked wrong. An extension
with `workspace: none` hears neither: a branch name is something the
repository knows about itself.

**What the two workspace signals can and cannot see.** They are built on
`git`, polled: `git rev-parse` at 1 Hz and `git status` at half that,
both skipped entirely while no extension entitled to the result is
subscribed. So an ignored file is invisible, and a project that is not a
repository produces no workspace signals at all. The alternative was a
filesystem watcher that would have had to learn `.gitignore` before it
was usable, and would have reported every write inside `node_modules`
until it did.

**A first observation is not a change.** The watcher learns where HEAD
is without announcing it, and forgets again when the last listener goes
away — otherwise a freshly subscribed extension is told HEAD "moved" to
where it has been since before it existed.

**Who hears a signal.** A process hears it over its loopback channel;
a page hears it through the window it is framed in. An extension with
neither — an `endpoint` service with no page — hears nothing: being told
is something igni does to a process it started or a page it frames.

**Subscription is a grant, not a filter.** The check happens where a
signal is dispatched, never where it is received — anything that posted
to every extension and asked each to discard what was not its own would
be trusting the extension to discard it, and the extension is the
untrusted party.

### The host object model

**An extension can do anything in igni that igni's own UI can do, and
see everything that happens in it** — `docs/HOST_OBJECT_MODEL.md` is the
model. igni is one tree of objects (`host.sessions`, `host.chat`,
`host.ui`, `host.plugins`, `host.workspace`, …) with properties, signals
and methods; an extension handles a signal and, while handling it, reads
and writes the model:

```python
@ext.on(ext.host.chat.run_completed)
async def review(run: Run) -> None:
    if run.changed_files:
        await ext.host.chat.post("Two issues in the files you just changed: …")
```

The process reaches it as `ext.host` (`igni-extension`) over a loopback
WebSocket igni gives it (`IGNI_HOST_URL`, guarded by the same per-process
secret as the token endpoint, `IGNI_HOST_SECRET`); the page reaches the
same model as `bridge.host` (`@igni/extension-bridge`) through the window
it is framed in. `docs/HOST_MODEL_PROTOCOL.md` is the wire, for SDK
maintainers only.

**Granted per object, by `permissions.host`.** One entry per object
(`chat.read`, `chat.post`, `sessions`, `ui`, …) or a `*`-suffixed prefix.
An object not granted does not exist for the extension: reads and
methods answer `not_granted`, and its signals never arrive.
`host.workspace` is granted by `permissions.workspace` and limited to
`workspacePaths`, like every other workspace fact; an extension's own
state, settings and notifications need no grant. A write goes through
the handler igni's own UI uses, so one the person could not make is
refused the same way.

**One schema, two SDKs.** The model is described once
(`ember_code.extensions.host.definition`, versioned under §8) and both
SDKs' `host` objects are generated from it (`make host-sdks`); a test
fails when they are stale, and another fails when igni gains an RPC, a
UI action or a broadcast with no member in the model and no reason it
has none.

**Messages from an extension are labelled.** `host.chat.post(text)` puts
a message in the chat with the extension's name on it, and the agent is
told who sent it before it answers; nothing posts as the person.
`host.chat.append(view)` shows a json-render block that is never sent to
the model.

The four topics under *Events* above are signals of the model whose wire
names are those topic strings (`sessions.current_changed` travels as
`session.switched`).

### Shipping one: from its service, through igni

An extension reaches people's machines **from its service and nowhere
else** — §8 of igni server's `service-identity.md` is the contract.
There is no extension marketplace and no group policy that pushes one.

1. **The service uploads it.** On every start, after registering, a
   service `PUT`s its bundle — a gzipped tar with `manifest.json` at
   its root — to `{igni}/v1/clients/{client_id}/extension`, with
   `X-Igni-Bundle-Signature`: a compact JWS, signed with the key the
   service registered, over `{"extension", "version", "sha256"}`.
2. **A super admin approves the service**, having compared its key's
   thumbprint and read what the extension would put on machines.
3. **Every signed-in igni installs it.** At session start-up and on
   sign-in the host lists `GET {igni}/v1/extensions` as the signed-in
   person, and for each entry whose version or digest differs from what
   is installed downloads `GET /v1/extensions/{id}/bundle`, verifies
   the JWS against the entry's `publisher_jwks` (RS256 or ES256, by
   `kid`), checks that the claim names this archive's SHA-256 and this
   manifest's id and version, unpacks it safely, and installs it through
   the ordinary boot path — trust, deployment profile, capability
   resolution. An extension whose service is no longer listed is
   stopped and removed.

**Never in the way.** The sync runs in the background. Not signed in,
igni unreachable, a bundle that does not verify — each is logged and
whatever is installed keeps running; a failed listing removes nothing.
(`extensions/distribution.py`)

**Trust is provenance.** A bundle igni distributed is trusted because
its signature verified against the approved service's key, and the
store records that (`<id>.distribution.json`). Everything else is a
development install. A manifest no longer carries a signature: one
cannot cover the archive it is inside, and the administrator-kept
Ed25519 trust store that checked it is gone.

**The `python` packaging** is how a service's extension runs: a package
speaking MCP over stdio, whose bundle carries its wheel and every
dependency's (`cp312` or pure-Python, per platform igni runs on).

```json
"python": {
  "package": "igni_artifacts_extension",
  "entry": "igni-artifacts-extension",
  "wheels": "wheels",
  "mcp": {"transport": "stdio"}
}
```

igni installs it into a private environment per extension and version,
made with its own interpreter (CPython 3.12), with `pip install
--no-index --find-links <wheels> --only-binary :all:` and pip's own
configuration ignored — an air-gapped machine has no index, and one
that has must not quietly use it — and starts `entry` over stdio.
(`extensions/python_env.py`)

**An extension's token for its service.** The extension never holds
the person's igni session. The host does the RFC 8693 token exchange at
`{igni}/v1/oauth/token` with the session as `subject_token` and the
service's URL as `audience`, caches the ten-minute token and renews it
in its last minute, and serves it on a loopback endpoint. A process
igni starts is given `IGNI_SERVICE_URL` (from the distribution record,
or the extension's `serviceUrl` setting for a development install),
`IGNI_SERVICE_TOKEN_URL` and `IGNI_SERVICE_TOKEN_SECRET`; `GET` with
`Authorization: Bearer <secret>` answers
`{"access_token", "expires_in", "service_url"}`, `401` for any other
secret, `503` when nobody is signed in. The secret decides the
audience, so an extension can only ever get its own service's token.
It is a listener of its own rather than the extension's UI origin,
which lives in a browser. (`extensions/service_token.py`) A service
token is never a `permissions.secrets` grant.

**Its home on the web.** The service's URL — the distribution record's
`service_url` — is the service's `client_uri`, which RFC 7591 defines as
the client's home page. igni shows it as **Open on the web** on the
extension's row in the Extensions panel and above the extension's page,
opened in the person's browser the way igni opens every external link,
and reports it as `web_url` in `host.extensions` and `host.self`. A
development install uses its `serviceUrl` setting when one is set; with
neither, no link is shown. (`extensions/web_home.py`)

**From the process, as the person.** `ext.service` (`igni-extension`)
is an HTTP client for the extension's own service that fetches,
attaches and renews that token; `ext.service.anonymous` reaches the same
service with no token, for what anyone may read; and
`ext.service.websocket(path)` opens a WebSocket to it with the token in
its `Authorization` header. `ext.tokens.token()` is the token itself and
`ext.tokens.signed_in` says whose it was. Whether anyone is signed in to
igni at all is `host.account`.

### Developing one: a directory or a repository

The only other way onto a machine, and only for development:
`IGNI_DEV_EXTENSIONS=1`, set by the person at the keyboard, for both
`igni ext install` and the backend that starts it.

```
IGNI_DEV_EXTENSIONS=1 igni ext install ./my-extension
IGNI_DEV_EXTENSIONS=1 igni ext install https://github.com/acme/tools --subdir extensions/scanner --ref v1.2.0
```

`--ref` takes a branch, a tag or a commit, and the commit that was
installed is always printed. `--subdir` picks one bundle out of a
repository holding several; the store keys by extension id, so two
bundles in one repository need different ids.

A development install of a `python`-packaged source checkout with no
wheelhouse builds one from the directory (`pip wheel`, network
allowed) and then installs from it exactly as a distributed bundle is
installed. A development install of an id igni also distributes wins
on that machine: the sync neither overwrites nor removes it.

**One repository, extension *and* plugin.** `contributes.plugin`
names a directory inside your bundle, so the plugin is not a
second thing to publish, version or hand anybody — it ships, and
is removed, with the extension. One plugin per extension: two
would be asking which agents belong to what.

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

  "trust": "first-party",             // first-party | partner | unsigned (dev only)

  // ── exactly one packaging: python, sidecar, endpoint or dev ──
  "python": {                         // how a service ships it — see "Shipping one"
    "package": "igni_codeindex_extension",
    "entry": "igni-codeindex-extension",
    "wheels": "wheels",
    "mcp": { "transport": "stdio" }
  },
  "sidecar": {
    "image": "igni__code-index:1.4.0", // in the bundle; never pulled at install
    "mcp": { "transport": "stdio" }
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
    "rpc": ["ext.igni.codeindex.*"],    // backend methods its own UI may call
    "host": ["sessions", "chat.read"]   // host-model objects it may reach — §1
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

**`rpc` and `host` are different lists, too.** `permissions.rpc` is
backend methods by their wire names — the extension's own service under
`ext.<id>.*`, and igni's internal methods, which can change with igni's
UI. `permissions.host` is objects of the host object model, which is the
promise: an extension that reaches igni through `host` is not broken by
igni refactoring its wire.

**Installed once.** One `id`, one install. No second copy, no per-project instance. Everything the extension needs to do, it does — two indexes is configuration inside the extension, not two installs.

**One version axis.** An extension declares a `contract` range and never names a product version. The deployment advertises one contract version, the lower of what the backend and the installed client implement.

### What an extension runs, it runs itself

**igni provides no infrastructure to extensions** — no graph database,
no vector store, no embedder. An extension that needs one runs it, from
the directory igni gives it alone (`IGNI_EXTENSION_DATA`, beside the
extension store and kept across reinstalls), or reaches its own service
for it. CodeIndex is the worked example: its local index is a Neo4j per
(project, commit), started by its own process on a JDK it downloads into
that directory, with the chunk vectors embedded by a sentence-transformer
in its own wheelhouse. None of it is igni's, and all of it leaves when
the extension does.

There used to be a `requires` field — a kind (`graph-db`, `embedder`, …)
that igni would provision on a laptop and bind in a deployment. Nothing
passed the installer a resolver, so an extension declaring it could not
install anywhere; the field is gone, and the manifest refuses it.

**What igni *does* provide is its floor**: identity and a token for the
extension's own service, the host object model, notifications, and its
decision model (`host.decide`). Those are the same everywhere, which is
why they are the floor and a database is not.

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
| **Deployment** | which services a super admin approved (and so which extensions igni distributes), which endpoints exist, contract version | no |
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
- **No `allow-popups`**, so nothing in the frame opens a window or a tab of its own — not a link with `target="_blank"`, not `window.open`. A page opens a link with `bridge.host.app.open_external(url)`: `https://` only, under the `links` permission, opened by its window the way igni opens every link outside it (HOST_OBJECT_MODEL, *Links out of igni*).
- **Per-extension CSP**: `default-src 'self'`, and `connect-src` is `'self'`, the extension's **own service** and its **declared egress** — each as `https://` and `wss://` origins, nothing wider (§6, *A page and its own service*). The bundle cannot open an undeclared socket, which is what makes egress auditable. The bridge is `postMessage` and is not governed by CSP at all, so "no `connect-src` beyond the bridge" — the earlier wording — meant `'none'` and would have blocked the extension from loading its own data files.

> **Why `allow-same-origin` is present**, measured in Chromium against two real origins rather than argued from the spec. Without it the frame has an opaque origin and **its own `panel.js` does not load** — for a module script the blocker is CORS rather than CSP, since from an opaque origin fetching its own file is a cross-origin request, and `'self'` would refuse the rest. The frame renders and nothing in it runs.
>
> The second reason is the one that decides the bridge. A `postMessage` from an opaque origin arrives at the host as `origin: "null"`, so the host cannot tell one extension from another, and the origin check every inbound message depends on has nothing to check. With a port each, `e.origin` is that extension's own `http://127.0.0.1:<port>`.
>
> These two bullets used to specify an opaque origin *and* `default-src 'self'`. They were coherent only while every extension shared one port, where an opaque origin was the only isolation on offer.
- **Bridge API, as built**: `rpc`, `navigate`, `close`, `params`/`onParams`, `settings`, `host`/`on` (the host object model and its signals), `service` (the extension's own service: `url`, `token()`, `fetch(path, init)`, `websocket(path)`), `setLocation`, `theme`/`onTheme`, `tokens`. Origin, source window and per-session nonce checked on every inbound message.

  `settings` is what `contributes.settings` is configured to, as an administrator left it. It is fixed for the life of the frame — a change applies at the next mount — so there is no `onSettings`. It is a separate namespace from `params` on purpose: `params` is *where you are*, replayed from `setLocation`, and this is *how you are set up*. One namespace would make a setting named `doc` and a link carrying `?doc=` the same key.
- **Bridge API, not built**: `identity`, `setTitle`, `setStatus`, `storage`, and per-call budgets or cancellation. This list used to be written in the present tense alongside the one above, which told an author that live push was solved. `HostMessage` is `hello | theme | params | result | host-reply | signal | service-token-reply`: a page subscribes to host-model signals at runtime with `bridge.on(signal, fn)`, granted by `permissions.host`, so following what happens in igni does not mean polling.

  `notify` means a message in *igni's* chrome. An extension's own toasts, inside its own frame, are published as `ToastHost`/`useToast` — the two are not interchangeable, and only something the user must see after navigating away belongs to the host.

  `notify` is not a bridge call and will not be one: notifications are the floor's (igni server's service identity document, §10). An extension's *service* tells other people through igni server (`POST /v1/service-notifications`); an extension's *process* tells the person at this machine with the MCP notification `notifications/igni/notify` on its stdio connection — `{kind, title, body, target: {page, params}, event_key}`, `target.page` one of its own pages, kept on this machine once per `event_key` and never sent to igni server. igni shows both in one notifications panel, opens a row by going to `target.page` with `target.params` as launch params, and puts the unread count per extension on its sidebar entry and chips. An extension does not compute a badge of its own (`src/ember_code/notifications/`).

  `storage` stays missing, but it is no longer the only way to keep a place. Each extension gets an ephemeral port, a port is part of an origin, so `localStorage` is wiped whenever the backend restarts. `setLocation` is the supported way to be restorable: the guest says where it is, the host folds that into its own URL and replays it in the next `hello`. Strings only, and small — it ends up in an address bar.

**A capability UI never holds the person's igni session.** What it may hold is a ten-minute token for its own service, which igni fetches for it (`bridge.service`), and it opens connections only to that service and to its declared egress. Everything igni does on its behalf goes through the host model.

**The dividing rule:** the iframe owns pages; json-render owns anything inline in the chat stream. Per-message iframes wreck scrolling, selection and performance.

Per business-ops **D16**, capability UI renders in the igni app only — never inside the VS Code or JetBrains host.

---

## 6. Security

The threat model is not a hostile stranger on a marketplace. It is **a trusted author's capability, built under deadline, inside a regulated estate, driving an agent that executes what it is told.**

### Trust

| Arrived | Trusted because | Installed by |
|---|---|---|
| distributed by igni server | its bundle's JWS verifies against the key of the service a super admin approved, and the archive is the one signed | the host, at start-up and sign-in, for every signed-in person |
| anything else — a directory, a clone | it is not; it is a development install | the person at the keyboard, with `IGNI_DEV_EXTENSIONS=1` |

The manifest's `trust` (`first-party` / `partner` / `unsigned`) is the author's label, shown to an administrator, and not consulted for trust: a bundle igni distributed is trusted whatever it says, and a development install needs the flag whatever it says. One rule hangs off it: `dev` packaging requires `unsigned` — and igni never runs `dev` packaging that arrived from a service.

**We do not sign, review or countersign anyone else's capability.** The host verifies the bundle is what the approved service signed — nothing more. The service's public key arrives with the listing from the customer's own igni server, so verification needs nothing outside their network.

**Only a super admin decides what is distributed**, by approving a service. No user install outside development, no marketplace fetch, no group policy push: software inventory is an accreditation control.

### Permissions

Declared, admin-granted, base-enforced, deny by default: `workspace` (none/read/write, path-scoped), `egress` (explicit hosts, empty by default), `secrets` (named grants the deployment resolves), `tools` (MCP tool names it may register with the agent), `rpc` (backend methods its UI may call over the bridge — exact names, or a `*`-suffixed prefix, because this list is read by whoever approves the install), `host` (objects of the host object model its process and page may reach, same grammar).

**Where each one is enforced, and how far.** This list was written before any of it was, and for a while the only enforced permission was `rpc` — which made the rest a list of restrictions that restricted nothing.

| Permission | Enforced at | How far it goes |
|---|---|---|
| `rpc` | `ExtensionRegistry.authorise`, on every call | Complete. |
| `host` | `HostModel.handle`, on every request from the process's socket and the page's bridge; and at dispatch, for every signal | Complete for what igni does on the extension's behalf: an ungranted object answers `not_granted` and its signals are never sent; signal contents are filtered by `workspace` and `workspacePaths`. |
| `tools` | tool registration, which is igni's | Complete. A service may expose what it likes; what reaches the model is this list. |
| `egress` | the page's `connect-src`, written from the manifest and the extension's own service, `https`/`wss` only; and the endpoint host igni itself dials, `https` only | **The UI, completely. Not the service's own process** — confining that needs an OS sandbox igni does not apply. A sidecar can still open its own sockets. |
| `workspace` | what igni *tells* a service, as `IGNI_WORKSPACE*` | An extension that was never granted the workspace is not told where it is. A service that goes looking anyway is not stopped — same OS-sandbox gap. |
| `secrets` | resolution: an undeclared name is never looked up, and the resolver is the deployment's | The value arrives in the service's environment as `IGNI_SECRET_<NAME>`. **Not a handle** — see below. |
| provenance | the distribution sync, before a bundle is stored, and the installer, which trusts only a recorded distribution | Complete. The JWS covers the archive's SHA-256, so changing any byte — a permission, a file — breaks it. |

**`decide` sends text to a model, but not off the machine.** `host.decide` runs igni's local decision model (Laya) on what the extension sends; nothing leaves the person's machine, and igni does not log the text. It is a grant like any other because it spends the person's CPU and memory — the first call loads a model of about 400M parameters — and because the Bypass guard shares the model: igni answers the guard first, so an extension's questions can wait but never delay an approval. Zero-shot accuracy is modest; an extension acting on an answer should have measured it on its own data (HOST_OBJECT_MODEL, *Typed decisions on this machine*).

**Secrets are values, not handles.** This section said "a handle, never a value" for as long as nothing implemented it. A handle needs an exchange step, and an extension's service is an MCP server igni *calls* — there is no channel for it to call back on. So the honest statement is the one above, and the handle exchange joins the list of things not built. What that costs: a secret is in the service's process environment, visible to anything that can read it there.

**The two gaps worth naming to a customer** are the same one twice: `egress` and `workspace` are enforced where igni decides, not where the extension acts. Closing them means running a sidecar under an OS sandbox — a network namespace and a mount namespace — which is the same work as container supervision and is not started.

A capability that asks for more on upgrade **does not get it silently**; it stays at its old grants until an admin accepts. Permission drift is how extension ecosystems rot.

### A page and its own service

An extension's page and process talk to **their own service** directly; the host is for host things. An extension is not stopped from calling anywhere else either — provided it says where, and the person approving it can read that. The rules:

- **`connect-src` is the page itself, its own service, and its declared egress. Nothing else.** The service is the one that shipped the extension — the distribution record's `service_url`, or a development install's `serviceUrl` setting — and needs no declaration: it is the extension's. Every other host is in `permissions.egress`, shown to the super admin on the Services page before approval, and enforced by the CSP (`Egress.csp_connect_src`). No service known, none in the CSP.
- **`https` and `wss` only.** Each origin is written with its scheme — `https://host` and `wss://host` — never scheme-less, which in a CSP matches any scheme. Plain `http`/`ws` only to this machine (`localhost`, `127.0.0.1`, `::1`) and only with `IGNI_DEV_EXTENSIONS` set; a plain-http service anywhere else is not given to the page at all (`ServiceOrigin.parse`). The same rule holds where igni dials an endpoint itself (`Egress.check_endpoint`).
- **Exact origins.** The manifest schema allows `host` or `host:port` — no scheme, path or wildcard — and the service contributes its origin, not its URL. There is nothing to widen.
- **The token goes to the extension's own service only.** igni fetches it with the same exchange as the process's (audience: the service's URL) and the page asks without naming an audience — the backend looks it up from the caller (`PageServiceTokens`). `bridge.service.fetch` and `.websocket` refuse a path that resolves to another origin, so the token is never attached to an egress host. An egress host is reachable, not trusted.
- **No `permissions.host` grant for it.** Having a service is the grant: the process has always been given this token, and the page is the same extension from the same signed bundle (see `docs/HOST_OBJECT_MODEL.md`, *An extension's own service is not the host*).
- **The rest of the frame's CSP is unchanged** — `default-src 'self'`, `script-src 'self'`, `object-src 'none'`, `base-uri 'none'`, `form-action 'none'`, `frame-ancestors` the host only. Only `connect-src` names the service.

**A browser socket says who it is in its first frame.** A browser's `WebSocket` cannot set an `Authorization` header, and a token in the URL is written to access logs, so `bridge.service.websocket(path)` opens the socket and sends `{"type": "auth", "token": …}` first — what igni's services read (the Artifacts document room). A service reading something else passes `authFrame`.

**HTTP from a page is cross-origin, and the service answers it for the token, not for the origin.** The page's origin is an ephemeral loopback port (`http://127.0.0.1:<port>`), so an allowlist of origins cannot name it. Instead every service built on igni-service answers cross-origin calls that carry `Authorization: Bearer` to routes that take a person's token (`igni.person`, `igni.optional_person`, `igni.org_admin`) with `Access-Control-Allow-Origin: *` — `BearerCors`, put on the app by `igni.install(app)`, with no CORS settings in the service. The token is the whole of the authority, so any origin is no wider than `curl`. **Credentials are never allowed**: no `Access-Control-Allow-Credentials`, and `bridge.service.fetch` sends `credentials: "omit"`, so the service's own website sessions (cookies, `igni.web.*`) are never reachable cross-origin; website routes, sign-in and igni's own calls give no cross-origin yes at all. The wire is service-identity §9.1.

Tested in `tests/test_a_page_reaches_its_own_service.py` and the bridge's `service.test.ts`.

### The agent is an attack surface

Tool descriptions, prompt fragments and tool results all land in a model's context, so a careless capability can steer the agent rather than merely misbehave.

- **Supplied text is data, never instructions** — delimited and origin-labelled on the way in, like any untrusted file content.
- **Changing the system prompt is allowed and visible, never silent.** An extension may replace igni's prompt or stand in for its agents — the deployment chose that by approving the service, after the Services page showed it exactly what is taken over. It cannot alter another capability's tool definitions, and what two extensions both want is decided by a super admin, never by load order.
- **Slot fills are length-capped** (8,000 characters); over-cap is refused and logged rather than crowding out igni's text.
- **Tools obey the permission pipeline and the hooks chain.** `BYPASS_PERMISSIONS` is unreachable from a capability.
- **Guardrails apply** on the same path as everything else.
- A result that *looks* like an instruction is still a result. Stated so a reviewer can test it.

### Egress

The air-gap claim is defended by a test (`tests/test_nothing_phones_home.py`). The contract extends it: every host declared, empty by default, UI egress no wider than its own service and its declared hosts, and each capability service carries its own equivalent of that test. Undeclared egress is a contract violation, not a bug — it is the one failure that costs a customer their accreditation.

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
| **Design tokens** | `PUBLISHED_TOKENS` in `packages/extension-bridge/src/tokens.ts` — colours, the spacing scale, the type scale, shape. Pushed live over the bridge, so an extension follows dark mode, light mode and a group's branding without asking. |
| **CSS classes** | The classes defined in `clients/ui/igni-ui.css` — `btn`, `chip`, `ui-card`, `ui-row` and the rest. Named deliberately the same as igni's own, so a change lands in both. |
| **React components** | The exports of `@igni/ui-react`. What the first two extensions actually import, plus the primitives any extension would need. |

These three are a promise. **Everything else about igni's appearance is not**, and the distinction is what keeps restyling the app possible: `theme.css` holds about two thousand classes and three dozen of them are published.

This replaces a line that read "the React component tree, CSS class names, any token not published as a theme variable" — written before there was a package, when no class name was published and the sentence was true. It stopped being true the moment `igni-ui.css` shipped, and a floor that contradicts what we hand people is worse than one that promises too much.

## Not in the floor

- Importing anything from `ember_code.*` — the contributions are the interface, not the package.
- **Any CSS class not defined in `igni-ui.css`**, any component not exported from `@igni/ui-react`, any token not in `PUBLISHED_TOKENS`. Naming a class is not the same as publishing it: an extension that reaches for `.kb-row` because it saw it in the DOM has taken a dependency we never offered.
- The markup those classes are applied to — which element, which nesting. The class is the contract; the tree under it is ours.
- `RpcMethod` members not namespaced to a capability.
- Database schemas, on-disk layouts.
- Base prompt text and base agent definitions.
- **Anything not named in this document.**

## 9. Developing one

- **The reference extension is `examples/reference-extension/`** — a compliance scanner, around 200 lines, written against the published surface and nothing else. It is the answer to "what does an extension look like", and a standing check on this contract: anything it has to hand-roll is something the floor is missing, and its README keeps that list.
- **`igni ext init <dir>`** scaffolds one that runs as written — manifest, an MCP server, a framed page with the bridge inlined so there is no build step, and a README. `igni ext doctor` checks a bundle and prints the contract version this igni implements; `igni ext install` (development only, behind `IGNI_DEV_EXTENSIONS=1`) copies it into `<data>/extensions/<id>/`, prints the permissions it is being granted, and the backend starts it at its next boot. `list` says where each one came from, and `remove` does what it says — one igni distributed comes back at the next sync while its service is approved.
- **An install outlives the process that made it**, which is why the CLI writes to a store rather than talking to a running backend: a CLI that needed one could install nothing before it started, and nothing would survive it stopping.
- **Dev mode** is `IGNI_DEV_EXTENSIONS=1`, the only thing that lets a bundle igni server did not distribute install or start, whatever trust it claims. An environment variable and not a setting a bundle can reach, because the easiest way to ship must not be the one with no provenance. The persistent banner and hot reload do not exist yet.
- **Shipping it** is the service's job: package it as `python` with its wheelhouse and have the service upload the signed bundle at start-up (§8 of igni server's `service-identity.md`).
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
| Prompt fragments, prompt replacement, slots and agent stand-ins as contributions | **shipped** (`extensions/contributions.py`, tied to `READY` rather than to being installed) |
| Impact and collisions, decided by a super admin | **shipped** (`extensions/impact.py`; igni server's Services page) |
| Manifest + validation | **shipped** (`docs/extension-manifest.schema.json` for the author's editor, `extensions/manifest.py` for the installer, held to each other by `tests/test_the_manifest_model_and_its_schema_agree.py`) |
| Distribution from services (signed bundle, JWS verified against the approved service's key, sync at start-up and sign-in) | **shipped** (`extensions/distribution.py`, `extensions/bundle_signature.py`) — the manifest `signature` field and the Ed25519 trust store are removed |
| `python` packaging, offline from the bundle's wheelhouse | **shipped** (`extensions/python_env.py`) |
| Service tokens by exchange, served on loopback | **shipped** (`extensions/service_token.py`) |
| `ext.<id>.*` RPC namespace | **shipped** (`extensions/registry.py`, `extensions/rpc.py`, `extensions/service.py`) |
| Sidecar supervision / install pipeline | not started — the registry takes a manifest and a bound service; nothing yet starts a container or registers an endpoint |
| Backend static origin, one port per extension | **shipped** (`extensions/origin.py`) |
| Sandboxed iframe + bridge | **shipped** (`extensions/ExtensionFrame.tsx`, `bridge.ts`, `protocol.ts`) — the host-side permission check now has its backend counterpart; nothing renders the frame yet |
| Policy cache keyed by `group_id` | not started |
| Extensions in group packs | **removed** — extensions come from their services through igni |
| Infrastructure for extensions (`requires`) | **removed** — an extension runs what it needs from `IGNI_EXTENSION_DATA` |
| Deployment profile: URL builder + the four raw-sqlite3 stores | not started |
| `igni ext init` / `doctor` | **shipped** |

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

**The install pipeline now populates it.** `ExtensionInstaller` starts a `python` or `dev` process or reaches an endpoint, binds it, and marks it `READY`; bundles arrive from their services through igni server's distribution and are verified before they are stored (see "Shipping one"). Starting a sidecar container is what is still missing.

## Reference

- Closed unions to open: [`clients/web/src/chat/pageStack.ts`](../clients/web/src/chat/pageStack.ts), [`clients/web/src/App.tsx`](../clients/web/src/App.tsx)
- Permissions: [`core/config/permission_eval/`](../src/ember_code/core/config/permission_eval), [`core/config/tool_permissions/`](../src/ember_code/core/config/tool_permissions)
- Group tier: [`core/config/group_policy.py`](../src/ember_code/core/config/group_policy.py)
- Store seams: [`core/db/urls.py`](../src/ember_code/core/db/urls.py), [`core/db/engine_registry.py`](../src/ember_code/core/db/engine_registry.py), [`core/session/_sqlite_utils.py`](../src/ember_code/core/session/_sqlite_utils.py)
- MCP: [`core/mcp/config.py`](../src/ember_code/core/mcp/config.py), [`docs/MCP.md`](MCP.md)
- Prompt placeholder to generalise: [`core/prompts/__init__.py`](../src/ember_code/core/prompts/__init__.py), [`core/agents/loader.py`](../src/ember_code/core/agents/loader.py)
- Air-gap enforcement: [`tests/test_nothing_phones_home.py`](../tests/test_nothing_phones_home.py)
- Commercial framing: `igni__business-ops/strategy/decisions.md` (D1, D3, D4, D7, D11, D16), `strategy/license-enforcement.md`
