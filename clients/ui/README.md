# igni-ui

The controls an extension draws with, so a surface built outside igni looks like one built inside it.

## Using it

Bundle `igni-ui.css` into your extension. Do not link it from igni's origin — your page runs under its own origin with `default-src 'self'`, so a cross-origin stylesheet is refused, and that refusal is deliberate.

```html
<link rel="stylesheet" href="./igni-ui.css" />
<body class="igni-root">
  <button class="btn btn-primary">Index this repository</button>
</body>
```

Then connect. The bridge applies the live theme before your first paint:

```ts
import { connect } from "@igni/extension-bridge";

const igni = await connect();          // tokens are on :root by now
const status = await igni.rpc("codeindex_status");
```

That is the whole setup. No build step is required beyond whatever bundles your own code.

## How theming works

Every value in the stylesheet is a token with a fallback.

The fallback is igni's dark palette. It is what you see if you open your page on its own, outside igni — so development does not require the app running.

Inside igni, the bridge pushes the **live computed values** at handshake and again whenever they change. Your page follows dark mode, light mode, and whatever a group's policy pack has branded, without you writing a line for any of it.

Values are pushed rather than baked in, on purpose. `theme.css` defines these across forty-five blocks — a `:root`, a light override, and a `prefers-color-scheme` block per feature. Any second copy of that is wrong within a release.

The published token list is in `clients/web/src/extensions/tokens.ts`, and it is **under the compatibility floor**: removing or repurposing one of those is a breaking change. Tokens not on that list — `--rail-width`, `--sidebar-w`, `--crumb-row-h` — describe igni's own chrome and are ours to change. An extension positioning itself against those would break the first time we moved something.

## What is here

Buttons (`btn`, with `-primary`, `-danger`, `-sm`, `-xs`, `-quiet`), chips, panels, fields, dialogs and the overlay behind them.

These are the classes igni itself uses. When one changes, both change — that is the point of sharing the names rather than copying the rules.

## What is not here, and why

No table, row, card, badge, empty state, spinner, tab or toolbar.

Not an oversight, and not a decision to leave extensions short. **igni does not have them either.** They exist as feature-scoped classes, written once per feature:

| Pattern | Times it is reimplemented in `theme.css` |
|---|---|
| `*-row` | 44 |
| `*-item` | 33 |
| `*-actions` | 32 |
| `*-title` | 27 |
| `*-empty` | 18 |
| `*-card` | 17 |
| `*-list` | 15 |
| `*-toolbar` | 10 |
| `*-table` | 10 |
| `*-badge` | 7 |

231 classes across 14 patterns — `kb-row`, `doc-row`, `codeindex-row`, `plugins-row`, and so on. 1120 of the stylesheet's 1159 classes are feature-scoped like this.

Publishing a `table` before deciding what a table *is* here would mean picking one of ten existing answers and making it the floor — where we cannot take it back, and where every extension inherits whichever we picked. The generalisation has to happen in igni first.

Until it does: write your own against these tokens. That is the honest answer, and it is why the tokens are published even where the components are not.

## Versioning

This package follows the extension contract version, not igni's. See [EXTENSIBILITY.md](../../docs/EXTENSIBILITY.md) §8 for what counts as breaking and the deprecation window.

## The spacing scale

`--space-1` through `--space-16` — 2, 4, 6, 8, 10, 12, 14, 16, 20, 24, 32.

Use them for `gap`, `margin` and `padding` and your layout sits on the same rhythm as the app around it, rather than next to it.

They are not an invention. They are the steps igni already used, written as literals in 375 places. 85% of gaps and 83% of margins landed on them exactly, which is why adopting them changed nothing on screen — and why the ~60 that stayed literal (`5px`, `3px`, `7px`, `18px`) are now visible as deviations rather than hidden among the rest.

## The type scale

`--text-2xs` through `--text-4xl` — 10, 11, 12, 13, 14, 17, 20, 30, 42.

Nine steps, down from twenty-two hardcoded sizes that included every half-step from 9px to 15px. `11.5px`, `12px` and `12.5px` between them accounted for 272 declarations: one size written three ways, not three sizes.

Collapsed by role rather than by ratio, because the clusters were already there. No value moved by more than 1px, and 98% of font sizes now name a step.

One deliberate exception survives, labelled where it lives: a digit inside a 12px circle stays at 7px, because 10px overflows it. A constrained space is a real exception. The half-pixel sizes were not.

## Shape

`--radius-sm` 6, `--radius` 10, `--radius-lg` 14, plus `--radius-pill` and `--radius-circle` for the two shapes that are not sizes.

`9999px` and `999px` were both in use for pills — identical to a renderer, two things to a reader. Now one.

**Padding is 62% on the scale, and the rest is probably right.** The values that stayed literal are overwhelmingly *odd* — 1, 3, 5, 7, 9, 11, 13px — which is the signature of optical correction against a 1px border rather than drift. Forcing them onto even steps would move things by a pixel for no reason. They are visible as literals now, which is the right state for a deliberate exception.

**Primitives beyond the controls above.** See the inventory earlier: 14 patterns are each reimplemented per feature, but they share far less than the names suggest. `*-row` agrees only on `display: flex`; `*-card` has 4 of 10 sharing a core. They do not duplicate so much as diverge, so there is nothing to extract — the design has to be decided, then features migrated to it. That changes pixels, so it is yours to call.
