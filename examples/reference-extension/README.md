# Reference extension — `acme.scanner`

What an igni extension looks like, end to end. A compliance scanner:
list policy violations, filter them, open one, accept it with a
justification, follow a scan while it runs.

Chosen because it touches the parts of the floor a real extension
touches — a table, a filter, a dialog with a form, a long-running job,
an empty state, and every way each of those can fail. **Anything
`src/` has to hand-roll is something the floor is missing**, which is
what this is for.

## The shape

```
manifest.json      what the extension is, and what it may do
index.html         the page igni frames
src/scanner.ts     the backend, typed — RPC names and wire checking
src/panel.tsx      the UI, which never learns an RPC name
```

The split is the first thing to copy. Everything that knows a method
name or a wire shape is in `scanner.ts`; `panel.tsx` renders state. The
day the backend renames a field, one file changes.

No build config here on purpose — bundle it however you like. The only
hard requirements are that `igni-ui.css` ships inside your bundle and
that you `await connect()` before rendering.

## Which manifest do I need?

`docs/extension-manifest.schema.json`, and your editor already knows
how to use it. `manifest.json` here opens with

```json
"$schema": "https://igni.sh/schema/extension-manifest-1.json"
```

which gives you completion, inline descriptions and red squiggles for
every field, including the ones whose absence is only an error in
combination — a `partner` build with no signature, both packagings at
once, a `sidecar` with no `image`.

Do not take this file as the authority on its own. It is validated
against that schema by
`tests/test_a_manifest_is_checked_against_its_schema.py` on every run,
and so is every other example in `examples/`. That test exists because
the first version of this manifest broke the schema in three places.

## What it demonstrates

**Theming costs you nothing.** You import `igni-ui.css` and call
`connect()`. The host pushes live token values before your first
paint, so the panel follows dark mode, light mode and whatever a
group's policy has branded, without a line about any of it.

**Check the wire, don't cast it.** `igni.rpc<T>()` is generic, and a
generic is a promise you make to the compiler, not a fact about the
bytes. Your backend is a separate process on a version the host does
not control — a sidecar image pinned at install, or an endpoint
somebody else deploys — so the two really do drift. `scanner.ts` parses
every response and throws a `WireError` naming the method, which is one
legible message instead of `Cannot read properties of undefined` three
components away.

**States are unions, not flags.** `ScanStatus` is
`idle | running | failed`. It started as
`{ running: boolean; percent: number | null; step: string }`, which
spells four states that cannot happen and leaves the one that matters —
*failed* — unspellable, so a scan that died reported itself as "not
running", which is also what finishing successfully looks like.

**Three load states, not a nullable list.** `violations === null` meant
"loading", so there was nowhere to put "the backend refused" — and a
denied RPC, which is the single most likely failure for a new
extension, rendered as an empty table forever.

**Poll by scheduling the next one, not on a clock.** `setInterval`
fires whatever is in flight, so a slow backend accumulates overlapping
requests whose responses land out of order and the list flickers
between two versions of itself. The loop here also backs off after a
failure: a refused RPC is refused every two seconds forever, which is a
lot of denials in an audit log for a panel nobody is watching.

**`Promise.allSettled`, not `all`.** The list and the status are
independent. `all` rejects on the first failure, so a backend that can
list but cannot report status showed nothing at all.

**Dialog state belongs to the dialog.** The draft justification lives
in `AcceptDialog` with `key={open.id}` on it, so opening a second
violation gets an empty box. Held in the parent, it showed the previous
violation's justification, ready to submit against the wrong row.

**Severity maps to the floor's tones**, not to your own colours. Four
tones is fewer than you want; using them is what makes your panel look
like part of igni rather than beside it.

**Progress with no percentage slides** rather than sitting, because a
bar parked at a fixed width reads as stuck.

**The dialog is behaviour, not a box.** Focus trapped, Escape closes,
focus returns to whatever opened it, a drag that starts inside does not
count as a backdrop click. You get all of that by using it. What you
still owe it is a destination for focus when the thing that opened it
no longer exists — accepting removes the row, so the panel puts focus
back itself.

**A `<tr>` with an `onClick` is not keyboard-reachable**, so the row
carries a real button — named `Review {rule}`, because twelve buttons
all called "Review" are twelve identical entries in a screen reader's
button list with no way to tell which row any of them belongs to.

## It is one shape, and a forgiving one

A single view, mostly reading, one pane, no prose. Each of those avoids
a known gap, so do not read this file as evidence the floor is
complete:

- **one view** never needs an addressable internal location. There is
  no way for an extension to say where it is, or to be told where to
  go after it has mounted — `params` arrives once, inside `hello`.
- **mostly reading** never needs an unsaved-work guard. `close()` is
  one-way with no veto, and `localStorage` does not survive a backend
  restart, so a draft has nowhere to live.
- **one pane** never needs a page shell or a split. `.igni-root` sets
  colour and type, not layout.
- **no prose** never needs a block markdown renderer. The floor
  publishes `MarkdownInline`, which is designed to *destroy* block
  structure for previews.

## What it cannot do yet

This compiles, is type-checked in CI, and **cannot run**. Two reasons,
neither of them about this code:

1. `@igni/extension-bridge` and `@igni/ui-react` are not published
   packages — they are a tsconfig alias inside `clients/web`.
2. Nothing installs an extension. `ExtensionRegistry` takes a
   validated manifest and `ExtensionRpc.bind` takes a service, but
   nothing starts a sidecar, registers an endpoint, or renders
   `ExtensionFrame`.

**The third reason is gone.** `ext.acme.scanner.*` used to be
unanswerable — `RpcMethod` is a closed enum validated exhaustively, so
every call here came back `Unknown RPC method`. The namespace is real
now: a call is parsed, checked against this manifest's
`permissions.rpc`, and routed to the extension's service as an MCP
tool call. The method igni calls is the final segment, so
`ext.acme.scanner.list` calls the tool `list` — your server's tool
names are plain, not namespaced.

That is also why `permissions.rpc` is worth getting right. It is
enforced by the backend against the installed manifest, not by the
frame: the host checks it too, but only so a refusal is immediate.
Narrow it and your own panel stops working; the refusal will tell you
which line to add.

## What it had to hand-roll

Kept as a list because it is the floor's to-do:

- **Table markup.** `Table` is a bare `<table>`; there is no
  `Thead`/`Tr`/`Td`, no `th`/`td` padding, no sortable header, no
  `aria-sort`.
- **A live region.** The result count is announced through a
  hand-written `aria-live` paragraph using `ui-visually-hidden`.
- **An error boundary.** EXTENSIBILITY §7 promises "UI throws → its own
  error boundary"; nothing provides one, so a throw today is a blank
  frame inside a working app.
- **A toast.** `notify` is named in §5 and does not exist.
- **Virtualisation.** A real scan returns thousands of rows.
