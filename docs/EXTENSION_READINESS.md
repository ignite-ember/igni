# Extension readiness

What is still missing before a partner can build a rich extension,
stated as gates with a check you can run rather than a feeling.

Every "missing" below was verified against the tree, not remembered.
Where something is marked **declared but unenforced**, that is the
dangerous category: a permission an author believes they granted and
nobody checks is worse than one that does not exist, because the
manifest reads as if it is protecting something.

**Today's one-line summary: one extension runs end to end, and four
of six permissions are enforced completely with two enforced at
igni's boundary rather than the extension's.** A bundle installs,
starts, serves its UI on its own origin, draws in a frame and answers
`ext.<id>.*` calls that are authorised against its manifest — proven
by a test that runs a real MCP server as a subprocess. What is still missing
is a CLI to install with, and OS-level confinement of a service's own
network and filesystem.

---

## Gate A — it runs at all

Nothing here is hard. All of it is missing.

| # | State | Note |
|---|---|---|
| A1 | ✅ mostly | `@igni/ui-react` and `@igni/ui-css` are packages that pack — 16 files, 31 kB, tests excluded. Not pushed to a registry; for an air-gapped deployment the tarball in the bundle is the delivery anyway. `@igni/extension-bridge` is still a `clients/web` alias. |
| A2 | ✅ | `list_extensions` tells the client what is installed, its state, its grant list and which loopback port its UI is on; `ExtensionPage` mounts `ExtensionFrame` for a page whose kind the registry says an extension owns. |
| A3 | ✅ | `ExtensionRpc` is constructed in `BackendApp` and shared by the boot router and every per-session one, so one registry serves the whole backend. |
| A4 | ✅ | `ExtensionInstaller` validates, checks trust, registers `PREPARING`, starts or reaches the service, serves the UI on its own origin, binds and marks `READY`. Failures after registration are `FAILED` with a reason. |

**What is not done in A4:** there is no CLI, so installing means
calling `ExtensionInstaller.install` rather than
`igni ext install ./my-extension` — that is D1/D2. Sidecar images
fail saying container supervision is not implemented; `endpoint` and
the new development packaging both work.

**A third packaging, `dev`,** was added because neither of the other
two can be run by an author on a laptop: one needs a container
registry and the other needs a deployed service. It is argv, runs
over stdio, and the schema refuses it on anything but
`trust: unsigned`.

Three things only a real subprocess found, each of which a stub had
agreed with:

- **The MCP first-use prompt blocked every extension.** It exists for
  a `.mcp.json` anyone can commit to a repo; an administrative
  install is not that, and the prompt fails closed when there is
  nobody to ask — so no extension could ever have started on a
  headless or shared backend.
- **`structuredContent` was the wrong preference.** A FastMCP tool
  annotated `-> str` wraps its answer as `{"result": …}`, so every
  such extension would have unpicked an envelope.
- **`cwd` never reached a stdio server**, and an extension's command
  is relative to the bundle it was installed from.

---

## Gate B — the permissions are real

The manifest models all of these and the schema validates them.
**Nothing reads any of them.** This is the gate that decides whether
the extension boundary is a security boundary or a comment.

| # | Declared | Enforced at | How far |
|---|---|---|---|
| B1 | `permissions.rpc` | `ExtensionRegistry.authorise` | ✅ complete |
| B2 | `permissions.egress` | the page's `connect-src`, written from the manifest; and the endpoint host igni dials | ⚠️ **UI complete, service not.** Confining a sidecar's own sockets needs an OS network namespace. |
| B3 | `permissions.secrets` | resolution — an undeclared name is never looked up, and the resolver is the deployment's | ⚠️ **value, not handle.** It reaches the service's environment as `IGNI_SECRET_<NAME>`. |
| B4 | `permissions.workspace` / `workspacePaths` | what igni *tells* a service | ⚠️ **told, not confined.** An ungranted extension is not told where the project is; one that goes looking is not stopped. |
| B5 | `permissions.tools` | tool registration, which is igni's | ✅ complete |
| B6 | `signature` | install, offline, Ed25519, against keys an administrator added | ✅ complete. The digest covers every file plus the manifest without its own signature, so widening a permission after signing breaks it. |

**The two partial ones are the same gap twice**: `egress` and
`workspace` are enforced where igni decides, not where the extension
acts. Closing them means running a sidecar under a network namespace
and a mount namespace — the same work as container supervision, and
not started. For a regulated deployment that is the honest sentence
to put in front of an assessor, and it is better than the previous
one, which was that none of it was enforced at all.

**Secrets became values rather than handles**, and the contract was
corrected rather than the other way round. A handle needs an exchange
step and an extension's service is an MCP server igni *calls*, so
there is no channel for it to call back on. The cost: a secret sits
in the service's process environment.

---

## Gate C — contributions land somewhere

The manifest can declare these. None of them reach the app.

| # | Contribution | State | Done when |
|---|---|---|---|
| C1 | `contributes.pages` | FE `ContributionRegistry` exists and is read by `App.tsx`; **nothing ever calls `install()`** | An installed manifest adds a sidebar entry and a page kind. |
| C2 | `contributes.menu` | same registry, same gap | The entry appears in the header menu and runs its command. |
| C3 | `contributes.commands` | not wired | The slash command is offered and dispatches to the extension. |
| C4 | `contributes.promptFragments` / `promptVariants` | `PromptFragments` exists and works, but only first-party CodeIndex registers; no manifest path | An extension's anchor is substituted into a prompt while it is `READY`, and withdrawn when it is not. |
| C5 | `contributes.settings` | modelled, never rendered | Settings appear in igni's own settings surface and arrive as launch params. |

---

## Gate D — somebody outside this repo can build one

| # | Missing | Done when |
|---|---|---|
| D1 | `igni ext init` | Scaffolds an extension that builds and runs on first launch. |
| D2 | `igni ext doctor` | Prints the deployment's contract version and what is wrong with a bundle, in the author's words. |
| D3 | Dev mode | Runs unsigned from localhost with a persistent banner and reload — the only place `trust: unsigned` is accepted. |
| D4 | A second reference extension | The current one is single-view, read-mostly, one-pane and prose-free. Each of those avoids a known gap, so it cannot be the check it claims to be. A document-shaped one would exercise `setLocation`, dirty state, a split and block markdown. |

---

## Gate E — components for a *rich* extension

Tier 0 shipped: layout, scroll, error boundary, toasts, banners, live
regions, selection, table internals, sortable headers, and the
`params`/`setLocation` round trip that makes an extension addressable.
46 exports, 100 published classes.

For rich, Tier 1 from `UI_FLOOR.md` is what remains. The ones that
actually block a shape rather than merely annoy:

| # | Component | Blocks |
|---|---|---|
| E1 | `Combobox` | Any search-and-pick. The hardest to hand-roll correctly and the most-wanted. |
| E2 | `Popover` | Anything anchored that is not a menu or a tooltip. |
| E3 | `Split` | Two-pane anything. Already written as `ColumnHandle`, 180 lines, unpublished. |
| E4 | `Markdown` (block) | Any document or prose surface. Only the inline renderer is published and it is *designed* to flatten block structure. |
| E5 | `Form` / `Fieldset` | Multi-field editing: submit semantics, error summary focus. |
| E6 | `Drawer` | Side panels; shares `Dialog`'s trap machinery. |

Tier 2 is genuinely optional and additive — never breaking, so
shipping without it costs nothing.

---

## Gate F — operable once it is running

| # | Missing | Done when |
|---|---|---|
| F1 | Health reporting | `ExtensionRegistry.set_state` exists; nothing reports into it. An extension that dies should show `FAILED` with a reason and a fix, not just stop answering. |
| F2 | Upgrade path | A manifest asking for more permissions stays at its old grants until an administrator accepts, and they are shown the diff. |
| F3 | Uninstall | Contributions withdrawn, prompt anchors removed, service stopped, port released. |
| F4 | Observability | An administrator can see what an extension called and what was refused. The refusals are already logged at info; nothing surfaces them. |

---

## What to do in what order

**To hand one trusted partner a thing that works** — Gate A is done,
which leaves D1/D2 (a CLI to install with) and F1. That is the
smallest honest deliverable: they can build, install, run, call their
own service and see it fail legibly. Gate B stays open and is written
into the agreement rather than the code.

**To let an untrusted third party build** — Gate B is done except
for the OS sandbox that `egress` and `workspace` need to bind on the
extension's side rather than igni's. That is the remaining honest
caveat, and it is one sentence rather than five.

**To call it rich** — Gate E, and Gate D4 to prove it, since a second
differently-shaped extension is the only thing that has ever found a
real gap in this floor.

**To sell it into a regulated deployment** — B6 is done. B2 is the
one left that gets asked about by name, and the answer today is "the
UI cannot reach an undeclared host; the service is trusted not to".
