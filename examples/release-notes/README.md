# Release Notes — `acme.notes`

The second shape, and the reason it exists.

`examples/reference-extension` is a **single view, mostly reading,
one pane, no prose**. Each of those four words hides a gap in the
floor, so it cannot be the standing check its own README claims to
be — it imports only from `primitives`, which is the half that was
already covered.

This is the opposite of all four: a two-pane browser over editable
documents with comments. Its job is to find what that costs.

```
manifest.json      what it is and what it may do
prompts/style.md   a file-sourced prompt fragment
src/notes.ts       the backend, typed — RPC names and wire checking
src/panel.tsx      the UI
ui/panel.css       everything the floor made it style itself
```

## What it found

**A published pair that could not be used as a pair.** `Tabs`
generated its own id prefix with `useId`, `TabPanel` *required* one,
and nothing let a caller learn what had been generated. Both are unit
tested — separately — and neither had a consumer anywhere in the
repository, so nothing noticed. `Tabs` takes an optional `base` now,
and a test renders the two together.

That is the whole argument for a second example, in one bug.

## What works, and could not before

- **`setLocation` / `onParams` make it addressable.** Picking a
  version writes it into the host's URL; a reload or a shared link
  comes back to the same note. Nothing else in the contract can do
  this — the frame cannot write the address bar, and its
  `localStorage` dies with the ephemeral port it is served from.
- **Selection has a published look.** `.ui-item[aria-selected]` means
  the thing that makes a row look picked is the thing that makes it
  announce as picked, so the two cannot disagree.
- **`AppShell` + `Scroll` make two independently scrolling panes
  possible at all.** Without `min-height: 0` the list grows the page
  instead of scrolling.
- **`ErrorBoundary`, `ToastHost`, `LiveRegion`** — all three were on
  the reference extension's hand-rolled list and are not on this one's.

## What it had to hand-roll, and what the floor took back

This example's first draft hand-rolled three things. Two of them are
published now, which is what it was built for:

- **The split** was forty lines of `panel.tsx` and a block of CSS —
  pointer capture, clamping, a width in a custom property so dragging
  does not re-render the document, and a keyboard-operable
  `role="separator"`. `Split` is published. It deliberately does
  *not* persist the width the way the app's `ColumnHandle` does,
  because an extension's `localStorage` dies with its ephemeral port;
  it hands the width back and this example puts it in the host's URL,
  which survives.
- **Block markdown** was a deliberately small subset that did not
  look like igni's prose, because the prose styling lived in
  `theme.css`. `Markdown` and `.ui-prose` are published.

Still ours:

- **An editor that fills its pane.** `Textarea` is a bare textarea
  with a `min-height`, and a document editor wants the pane.

## The gap that is not a component

**There is no way to stop the host navigating away from a dirty
draft.** `close()` is one-way and `HostMessage` has no "about to
close" for a guest to veto, so `beforeunload` covers a reload of this
frame and nothing covers the case that actually happens. An extension
can protect a draft from the browser and not from its host, which is
the wrong way round.

Autosave is the workaround and it is a real one — a document that
only saves on a button is a document somebody loses — but it is a
workaround. The fix is an envelope kind, additive like `params` and
`location` were.

## What it demonstrates about writing one

- **Check the wire.** `igni.rpc<T>()` is a promise to the compiler,
  not a fact about the bytes.
- **A conflict is an outcome, not a fault.** `save` answers
  `{ conflict: "<their revision>" }` and the client turns it into a
  typed error that keeps your text. Replacing the draft with theirs
  would be the data loss the check exists to stop.
- **Remount on identity.** `<NoteView key={version}>` is what resets
  the editor. Holding the draft above it would show the previous
  release's text against the new one's revision, and the first save
  would overwrite it.
- **A file-sourced prompt fragment** (`prompts/style.md`) teaches the
  agent this repository's house style. Contained to the bundle:
  `from` is a string out of a manifest.

## What does not work yet

One thing, and it is not about this code:
`@igni/extension-bridge` and `@igni/ui-react` are a tsconfig alias
inside `clients/web` rather than an install, so `ui/panel.js` is not
built here.

The **backend** runs. `server.py` is a real MCP server and
`tests/test_a_second_extension_shape_works.py` installs this bundle
and calls it, including the revision conflict — which is the part
worth copying.

```bash
igni ext doctor examples/release-notes
IGNI_DEV_EXTENSIONS=1 igni ext install examples/release-notes
```
