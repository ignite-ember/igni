# Extension readiness

What is still missing before a partner can build a rich extension,
stated as gates with a check you can run rather than a feeling.

Every "missing" below was verified against the tree, not remembered.
Where something is marked **declared but unenforced**, that is the
dangerous category: a permission an author believes they granted and
nobody checks is worse than one that does not exist, because the
manifest reads as if it is protecting something.

**Today's one-line summary: the foundation is sound and nothing is
standing on it.** The contract, its enforcement and the component
floor are real; no code path installs, mounts or runs an extension,
so all of it is reachable only from tests.

---

## Gate A — it runs at all

Nothing here is hard. All of it is missing.

| # | Missing | Done when |
|---|---|---|
| A1 | `@igni/ui-react`, `@igni/ui-css`, `@igni/extension-bridge` have no `package.json`. They are a tsconfig alias inside `clients/web`. | `npm install @igni/ui-react` works from an empty directory — or, for air-gapped, a tarball in the bundle does. |
| A2 | Nothing renders `ExtensionFrame`. Verified: no `<ExtensionFrame` outside tests. | Opening a `contributes.pages` entry mounts the frame and the extension paints. |
| A3 | Nothing constructs `ExtensionRpc`. All three `MessageDispatcher` sites omit it, so a real `ext.*` call hits the "this backend has no extension support" branch. | A call from a mounted frame reaches a service and the value comes back. |
| A4 | Nothing populates `ExtensionRegistry`. No sidecar is started, no endpoint registered. | `igni ext install ./my-extension` leaves it `READY` and visible in the UI. |

**A4 is the keystone.** Until it exists, A2 and A3 have nothing to
show and every other gate is untestable end to end.

---

## Gate B — the permissions are real

The manifest models all of these and the schema validates them.
**Nothing reads any of them.** This is the gate that decides whether
the extension boundary is a security boundary or a comment.

| # | Declared | Enforced by | Done when |
|---|---|---|---|
| B1 | `permissions.rpc` | ✅ `ExtensionRegistry.authorise` | — shipped |
| B2 | `permissions.egress` | **nobody** | `connect-src` is widened from the manifest instead of hardcoded, and a sidecar's network is confined to the declared hosts. `origin.py`'s own docstring already promises the first half. |
| B3 | `permissions.secrets` | **nobody** | A handle resolves through a broker and the value never reaches the extension's process image or logs. |
| B4 | `permissions.workspace` / `workspacePaths` | **nobody** | A read outside the declared globs is refused, and `write` without the grant is refused. |
| B5 | `permissions.tools` | **nobody** | An MCP tool the manifest did not declare is not registered with the agent. |
| B6 | `signature` | **nobody** | Install verifies the detached signature against the deployment's trust store, offline, and refuses `unsigned` unless the deployment opted in. |

B2–B6 are the difference between "an extension is sandboxed" and
"an extension is sandboxed in the README". A partner pilot can run
with them open *if the partner is trusted and it is written down*.
They cannot ship to a regulated customer that way — undeclared egress
is the one failure that costs an accreditation.

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

**To hand one trusted partner a thing that works** — Gate A, plus
F1. That is the smallest honest deliverable: they can build, install,
run, call their own service and see it fail legibly. Gate B stays
open and is written into the agreement rather than the code.

**To let an untrusted third party build** — Gate B in full. Nothing
else on this page matters as much, because every item in it is a
permission that currently protects nothing.

**To call it rich** — Gate E, and Gate D4 to prove it, since a second
differently-shaped extension is the only thing that has ever found a
real gap in this floor.

**To sell it into a regulated deployment** — B2 and B6 are the two
that get asked about by name in an accreditation review.
