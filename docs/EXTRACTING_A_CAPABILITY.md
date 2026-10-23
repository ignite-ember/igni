# Extracting CodeIndex and Artifacts

What each capability surfaces today, which of it the extension floor
can express, and what is missing. Written before either is extracted,
because the point of extracting them is to find out whether the floor
is real — and finding out after the move is finding out too late.

Everything here was measured from the tree, not remembered.

---

## 1. The size of the thing

| | CodeIndex | Artifacts |
|---|---|---|
| `RpcMethod` members | 7 | **57** |
| Agent tool | `codeindex` | `igni_artifacts` |
| Frontend panels | 1 | **31** |
| Prompt anchors | `{{CODEINDEX_GRAPH_SCHEMA}}`, `{{CODEINDEX_TOOLS}}` | none |
| Agent variants | `.codeindex.md` | none |
| Import cost, standalone | **106 MB** | **137 MB** |

The whole protocol is 161 methods, so **Artifacts is 35% of igni's
RPC surface**. That number is the argument for extracting it and the
reason it will not be quick.

---

## 2. What the floor already covers

| What it does today | Contribution | Fine? |
|---|---|---|
| A page in the sidebar | `contributes.pages` | ✅ |
| `/codeindex`, `/artifacts` in the tools menu | `contributes.menu` | ✅ |
| Slash commands | `contributes.commands` | ✅ |
| `{{CODEINDEX_GRAPH_SCHEMA}}` | `contributes.promptFragments`, `source: tool` | ✅ — this is the case the fragment system was built for |
| `.codeindex.md` agent variants | `contributes.promptVariants` | ✅ |
| The `codeindex` / `igni_artifacts` agent tools | MCP, gated by `permissions.tools` | ✅ |
| Reading the project | `permissions.workspace` | ✅ |
| Its own RPCs | `ext.<id>.*`, gated by `permissions.rpc` | ✅ mechanically — see §4 |
| A Neo4j database | `sidecar.requires: graph-db` | ✅ declared; **the resolver is not built** |

---

## 3. What the floor cannot express

Each of these is something a capability does **today**, in host
chrome, with no contribution type for it. They were found by grepping
for capability calls outside the capability's own panels — which is
the check this document exists to be.

### 3.1 Status indicators — `CodeIndexIndicator` — **closed**

`App.tsx:3652` renders `CodeIndexIndicator` in the composer's chrome,
beside `WatcherIndicator` and `GroupIndicator`. It shows index
freshness at a glance and opens the panel when clicked.

`indicators` was in EXTENSIBILITY §1's table and was moved to **"not
in v1"** on the grounds that nothing specified it. That reasoning was
right about the specification and wrong about the need: the *first*
extension uses one.

**Shipped.** `contributes.indicators` declares a chip; the host polls
the named tool for `{ label, tone, detail }` and draws it with the
published `Status`. Data rather than a frame, because a chip is
twenty pixels — and because it puts the judgement where it belongs:
`classify()` in the existing component is 90 of its 127 lines and is
entirely CodeIndex's opinion about its own states. A tone outside the
four the design system publishes is refused and the chip is not
drawn, so an extension cannot put a colour igni does not have into
igni's chrome.

### 3.2 Notifications — the host's notification panel

`components/NotificationsPanel.tsx:55` calls `artifact_notifications`
and `artifact_mark_seen`. That panel is host chrome, not an artifacts
surface: it is where *everything* notifies.

There is no `contributes.notifications`, and §5 lists `notify` among
the bridge APIs that do not exist. `ToastHost` is published and is
not this — a toast is inside the extension's own frame, and this is
the host telling somebody that a colleague commented on a document
they are not looking at.

### 3.3 Push — `artifact_waiting` — **closed**

`server_artifacts.py:3126` broadcasts on channel `artifact_waiting`;
`App.tsx:724` and `NotificationsPanel.tsx:79` subscribe.

`HostMessage` is `hello | theme | params | result`, so an extension
can only poll. §5 says so honestly. But Artifacts is a collaborative
surface — a comment arriving is the event the whole feature is about
— and polling a 2-second interval for it is not the same product.

**This was the gap that mattered most**, because it is the one an
extension cannot work around — layout can be hand-rolled, a missing
event cannot.

**Shipped.** `permissions.events` grants host topics from a
published `HostTopic` vocabulary; an extension's own events, under
`ext.<id>.`, need no grant and another's are unreachable. The check
happens at dispatch rather than delivery, because a host that posted
everything and asked each frame to discard what was not its own
would be trusting the frame to discard it.

All four host topics now have producers: a git watcher behind the
two `workspace.*` ones, the pool's `attach_session` behind
`session.switched`, the registry behind `extension.state-changed`.
Each listener gets its own payload, so a `workspace.files-changed`
is filtered to that extension's `workspacePaths` before it is sent.

What Artifacts still needs is its *own* events relayed from its
service to its frame — the `ext.<id>.` half — which the envelope
carries and nothing yet produces. And delivery ends at an iframe,
so an extension with no page hears nothing at all; a service is
told about the host by being called.

### 3.4 Cross-user realtime

`useDocumentRoom` holds "one room per document, shared between the
windows watching it", with presence. Within one backend that is a
broadcast. Between two *people* it is not, and on a cloud deployment
of one-process-per-user it is not either.

Not a floor gap so much as a deployment one — recorded in
`EXTENSION_READINESS.md` as G5 — but it lands on Artifacts first and
should not be discovered during the extraction.

---

## 4. Things that are fine mechanically and large in practice

- **57 RPCs become `ext.igni.artifacts.*`.** The namespace handles it
  and `permissions.rpc` is one `ext.igni.artifacts.*` line. What it
  costs is that every one of the 31 panels changes its call site, and
  the base protocol loses 35% of its surface — which is the point,
  but it is a large diff and not a careful one.
- **31 panels move to the extension's own frame.** They are the
  largest consumer of the UI floor there will ever be, which is what
  makes them the real test of it.

---

## 5. What blocks extraction today

Not the readers, and not the size. Two module-scope imports:

```
core/agents/builder.py:23   from ember_code.core.tools.artifacts import ArtifactTools
core/tools/tool_spec.py:36  from ember_code.core.tools.codeindex import CodeIndexTools
```

Building *any* agent drags in the whole capability. An import blocker
over either subtree stops the backend starting, which is the
mechanical definition of "not separable".

`ToolSpecCatalog` was already split into `base()` and `default()` for
exactly this, and the split sits underneath an unconditional import,
so it buys nothing until the edges are cut.

Cutting them is small, and it is the first thing.

---

## 5b. Where CodeIndex has got to

`igni__code-index/extension/` exists and installs.
`tests/test_the_codeindex_extension_runs_on_the_floor.py` drives the
real bundle — not an example — and it is skipped when that repository
is not checked out beside this one.

What arrives from its manifest, each replacing something hardcoded
here: the page, the menu entry, `/codeindex`, the status chip,
`{{CODEINDEX_GRAPH_SCHEMA}}` and the `.codeindex` agent variant. Its
`classify()` moved with it, which is the point — 90 of that
component's 127 lines were CodeIndex's opinion about its own states.

What has **not** moved is the local index, and with it
`codeindex_cypher` and six of the seven RPCs. They are not declared
in the manifest rather than stubbed. So igni still answers Cypher
queries itself, and the extension contributes everything else.

That is also why the import edges in §5 still matter: the index
cannot move while `tool_spec.py` imports it unconditionally.

## 6. The order

1. **Cut the two import edges**, with a test that blocks the subtrees
   and asserts the backend still starts. Until that passes, nothing
   else is checkable.
2. **Close the floor gaps in §3** — `indicators`, `notifications`,
   and push. Push is the hard one and the one worth doing properly,
   because it is additive to the envelope exactly as `params` and
   `location` were.
3. **CodeIndex first**, because it is 7 RPCs and 1 panel against 57
   and 31. If the floor is wrong, it is cheaper to find out there.
4. **Artifacts second**, as a service rather than a sidecar — it is
   multi-tenant, it is the one capability a group genuinely shares,
   and a cloud deployment wants one of it rather than one per user.

## 7. What "nothing is lost" means

A capability is extracted correctly when, with the extension
installed, all of this is still true and none of it is special-cased
in the host:

- its page is in the sidebar and its menu entry is in the tools menu
- its slash commands run
- its agent tools reach the model, and only the declared ones
- its prompt anchors are substituted, and withdrawn when it is down
- its agent variants take over, and stop when it is down
- its indicator shows in the composer chrome **(needs §3.1)**
- its notifications reach the host's panel **(needs §3.2)**
- a colleague's comment arrives without a poll **(needs §3.3)**
- uninstalling it removes every one of the above and leaves a
  working app

The last line is the test. A host that still mentions `codeindex`
anywhere after its extension is removed has not finished extracting
it — and `App.tsx` mentions it in six places today.
