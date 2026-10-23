# `@igni/ui-react`

The React components an igni extension is allowed to build on.
Everything exported here is under the compatibility floor: removing
one, renaming it, or changing a prop's meaning is a breaking change
under `docs/EXTENSIBILITY.md` §8.

```bash
npm install @igni/ui-react @igni/ui-css
```

```tsx
import { AppShell, Scroll, Table, Toolbar } from "@igni/ui-react";
import "@igni/ui-css";
```

## Peer dependencies

You already have `react` and `react-dom`. `react-markdown` and
`remark-gfm` come with `MarkdownInline`.

`mermaid` is **optional** and loaded on demand — a page with no
diagram in it never downloads the 700KB, and an extension that never
renders one does not need the dependency at all. Install it only if
you use `MermaidBlock`.

## The stylesheet is not optional

`@igni/ui-css` must be bundled into your extension, not linked from
igni's origin. Your page runs under its own origin with
`default-src 'self'`, so a cross-origin stylesheet is refused — and
that refusal is deliberate.

Inside igni the bridge pushes live token values before your first
paint, so the same markup follows dark mode, light mode and whatever
a deployment has branded. Standalone, the fallbacks in the stylesheet
are igni's own values, checked against them by a test.

## Source, not a build

This package ships TypeScript source. An extension bundles its own
React and runs its own build, so shipping a pre-compiled artifact
would mean pinning a JSX runtime and a target you may not share —
and it would put a second copy of these components in your bundle
next to the one your bundler already tree-shook.
