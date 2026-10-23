# Reference extension — `acme.scanner`

What an igni extension looks like, end to end. A compliance scanner:
list policy violations, filter them, open one, accept it with a
justification, follow a scan while it runs.

Chosen because it touches every part of the floor a real extension
touches — a table, a filter, a dialog with a form, a long-running job,
an empty state. **Anything `src/panel.tsx` has to hand-roll is
something the floor is missing**, which is what this is for.

## The shape

```
manifest.json      what the extension is, and what it may do
index.html         the page igni frames
src/panel.tsx      the whole UI, ~200 lines
```

No build config here on purpose — bundle it however you like. The only
hard requirements are that `igni-ui.css` ships inside your bundle and
that you `await connect()` before rendering.

## What it demonstrates

**Theming costs you nothing.** You import `igni-ui.css` and call
`connect()`. The host pushes live token values before your first
paint, so the panel follows dark mode, light mode and whatever a
group's policy has branded, without a line about any of it.

**Severity maps to the floor's tones**, not to your own colours. Four
tones is fewer than you want; using them is what makes your panel look
like part of igni rather than beside it.

**Progress with no percentage slides** rather than sitting, because a
bar parked at a fixed width reads as stuck.

**The dialog is behaviour, not a box.** Focus trapped, Escape closes,
focus returns to whatever opened it, a drag that starts inside does
not count as a backdrop click. You get all of that by using it.

**A `<tr>` with an `onClick` is not keyboard-reachable**, so the row
carries a real button instead.

## What it cannot do yet

This compiles against the real published surface and **cannot run**.
Three reasons, none of them about this code:

1. `@igni/extension-bridge` and `@igni/ui-react` are not published
   packages — they are a tsconfig alias inside `clients/web`.
2. Nothing renders `ExtensionFrame`, so no extension is mounted.
3. `ext.acme.scanner.*` cannot be answered. `RpcMethod` is a closed
   enum validated exhaustively, so an extension's own service has no
   namespace to reply on, and every `igni.rpc` call here would be
   refused.

Until (3) lands an extension's UI can only render igni's own data back
to the user. That is the highest-value item in Track A.

## What it had to hand-roll

Kept as a list because it is the floor's to-do:

- **Table markup.** `Table` is a bare `<table>`; there is no
  `Thead`/`Tr`/`Td`, no sortable header, no `aria-sort`.
- **Severity ordering.** No sort affordance of any kind.
- **An error boundary.** EXTENSIBILITY §7 promises "UI throws → its own
  error boundary"; nothing provides one.
- **A toast.** `notify` is named in §5 and does not exist.
- **Virtualisation.** A real scan returns thousands of rows.
