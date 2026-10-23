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
import { connect } from "@igni/extension-bridge";  // not published yet — see below

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

These are the same class names igni itself uses, so a surface built here reads as part of the app rather than beside it.

**They are copies, not one file**, and two tests now hold them to each other.

`ui-react/styled.test.ts` reads every `className=` expression in the published package and fails on the first name this file does not define. It also checks every fallback value here, `:root` and inline, against `theme.css`. Generating this file from `theme.css` is still the better fix and is still not done; until it is, these are what stop a difference being invisible.

They were written after finding two, neither of which was drift in the sense of a value slowly diverging:

- **Seven components were never covered at all.** `OverflowMenu`, `Tooltip`, `Skeleton`, `Monogram`, `MarkdownInline`, `MenuIcon` and the mermaid block named thirty-two classes between them that only `theme.css` defined. Inside the app they looked right — the one environment they are promised for is the one nobody rendered them in. The reference extension did not catch it either, because it happens to import only from `primitives`, which was the covered half.
- **Eighteen of fifty token fallbacks were a different value from the theme they claim to copy.** Radii at 6/10/14 against the app's 8/12/18, every semantic colour a different hue, a different UI font stack. Standalone is how an extension author develops before they ever mount the thing, so that is what they were tuning against.

`btn-warn` — a typed `variant="warn"` rendering as a plain button because the class existed in the app and not here — is the case the scanner cannot see by reading source, since no line contains the string `btn-warn`; it only exists as `btn-${variant}` at runtime. Those names are enumerated explicitly in the test for that reason.

Treat a difference as a bug and report it.

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

## Primitives

`ui-row`, `ui-actions`, `ui-list`, `ui-toolbar`, `ui-card`, `ui-item`, `ui-badge`, `ui-empty`, `ui-table`.

These were decided, not extracted. There was very little duplication to extract: the 231 classes named `-row`, `-card`, `-actions` share almost nothing — `*-row` agreed only on `display: flex`, and deduplicating all fourteen patterns saved 84 declarations out of 17,789 lines. They did not repeat, they diverged, each feature choosing its own gaps and paddings because there was no scale to choose from.

So where the existing uses agreed, the agreement became the value. Where they split, the split is resolved once with a reason rather than per feature. `ui-empty` is the clearest: its colour was split exactly eight and eight between `--fg-faint` and `--fg-muted`. Both clear WCAG AA since F138, so the tie breaks on meaning — an empty state is text you must read to know what to do next, which makes it content rather than de-emphasised chrome.

There is deliberately **no `ui-title`**. Titles varied across four type steps, and that variation is the design — a heading level, not drift. Use the type scale and a weight.

**Remaining:** igni's own 231 feature classes still carry their own copies. The primitives exist and are what an extension builds against; migrating the app onto them moves pixels in 64 files, and is worth doing behind the visual suite rather than blind.

## Not installable yet

`@igni/extension-bridge` and `@igni/ui-react` are a tsconfig `paths` entry and a Vite alias inside `clients/web`. There is no package.json, no registry entry, no build. **You cannot `npm install` either of them today**, and vendoring them by hand means maintaining a fork of the thing that was handed to you as a compatibility floor.

This is a build change rather than a code change, and it is the first thing to do before anyone outside this repository is asked to build against this.

## What an extension still cannot do

Worth knowing before you design around it:

- **Call its own backend.** `RpcMethod` is a closed enum and the router validates against it exhaustively, so there is no namespace an extension's own service can answer on. Your panel can call igni's methods; it cannot reach your sidecar.
- **Receive a push.** The host sends `hello`, `theme` and `result`. Following a long-running job means polling an RPC on an interval — which is what igni's own CodeIndex panel does.
- **Be mounted.** Nothing renders `ExtensionFrame` in the app yet, and no manifest loader exists.

These are named here rather than left to be discovered three days into a build.
