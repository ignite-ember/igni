# The UI floor — what an extension can build with

What `@igni/ui-react` and `igni-ui.css` must contain before "build any
extension" is true, and how a deployment restyles all of it without
forking.

Status today: **46 exports, 50 tokens, 100 published classes**, with
Tier 0 complete.

It was 34 exports and no layout at all, which was enough for one pane
of forms and a table — exactly the shape of the reference extension,
and why the gap was not obvious from looking at it.

Companion documents: `DESIGN_SYSTEM.md` (how the scales and primitives
were extracted, and what that cost), `EXTENSIBILITY.md` §8 (what the
compatibility floor promises).

---

## 1. The rule for what goes in

Publishing a component is a two-release promise under §8. So the bar
is not "an extension might want this".

> **The floor publishes behaviour that is dangerous to re-implement,
> and tokens. It does not publish shapes.**

A *behaviour* is something a competent engineer under deadline ships
an accessibility bug in: focus trapping, roving tabindex,
`aria-describedby` wiring, a live region, a virtualised list's
`aria-rowcount`. A *shape* is something that merely looks wrong when
hand-rolled; the fix for a shape is a token and a recipe.

That rule already predicts the current floor — `Dialog`, `Tabs`,
`Field`, `Switch`, `Progress`, `Spinner` are all behaviour — which
suggests it is the rule we were following without having written it
down.

Two additions to it, both learned the hard way:

- **It must render outside this repository.** Seven components shipped
  naming 32 classes only `theme.css` defined. `styled.test.ts` now
  fails on the next one.
- **It must not drag the app behind it.** `SpecBlock` loaded
  `../components/JsonRenderView` dynamically — a path with no
  existence in a published package. `boundary.test.ts` now fails on
  the next one.

---

## 2. The catalogue

`✅` published · `🔧` exists in-app, not published · `❌` does not
exist · `⛔` deliberately refused

### 2.1 Layout and structure

The largest hole. `.igni-root` sets colour and type and nothing else —
no `height`, no `box-sizing`, no `html`/`body` rules. Every full-frame
extension writes the same six lines, differently.

| Component | State | Why it is floor |
|---|---|---|
| `AppShell` | ✅ | Header/body/footer rows, `height: 100%`, `box-sizing` reset. The six lines everybody writes. |
| `Scroll` | ✅ | A scroll container with `min-height: 0` — the flexbox trap that makes a pane grow instead of scroll. |
| `Split` | 🔧 `ColumnHandle.tsx`, 180 lines | Pointer capture, clamping, persisted width, a keyboard-operable `separator` with `aria-valuenow`. Wanted by every two-pane surface. Pure behaviour. |
| `Stack` / `Inline` | ❌ | Gap on the scale, so spacing cannot drift off it. |
| `Grid` | ❌ | Responsive columns against the space scale. |
| `Row` | ✅ | |
| `Toolbar` | ✅ | |
| `Card` | ✅ | |
| `Panel` | ✅ (CSS only) | Needs a React wrapper. |
| `Section` | ❌ | Heading + body + optional actions, with the heading level wired to the outline. |
| `Resizer` | ❌ | The bare separator behind `Split`. |
| `Sticky` | ⛔ | Two CSS lines. |

### 2.2 Navigation

| Component | State | Notes |
|---|---|---|
| `Tabs` / `TabPanel` | ✅ | Roving tabindex, `aria-controls`, focus follow. Done properly. |
| `Breadcrumbs` | ❌ | `Row` of quiet buttons plus a separator — borderline shape, but the `aria-current="page"` is the bit people miss. |
| `Pagination` | ❌ | Page-count announcement is the behaviour. |
| `Stepper` | ❌ | Wizard state + `aria-current="step"`. |
| `NavList` | ❌ | Sidebar navigation with `aria-current`. |
| `Link` | ❌ | External-link affordance, `rel="noreferrer"` by default, and the "opens in a new tab" announcement. |
| `OverflowMenu` | ✅ | Submenus, focus, tick state. |
| `ContextMenu` | ❌ | Right-click *and* `Shift+F10`/context key, which is the half always omitted. |
| `CommandPalette` | 🔧 in-app | Filtering, roving focus, announced result count. |
| `Tree` | ⛔ | 855 lines in `ArtifactTreeView`, mostly artifact-specific. Every consumer wants a different quarter of lazy-load / multi-select / drag / filter-while-collapsed. Publish the **roving-tabindex recipe** and a selected state instead. |

### 2.3 Data display

| Component | State | Notes |
|---|---|---|
| `Table` | ✅ (bare `<table>`) | Three CSS declarations. No cell padding, no header rule. |
| `DataTable` | ❌ | Sortable headers with `aria-sort`, sticky header, row selection, column resize, zebra, empty and loading states. **The single biggest win.** |
| `SortableTh` | ✅ | The sort button and its announcement. |
| `VirtualList` | ⛔ as a component / ❌ as a contract | We must not pick a partner's windowing library. We owe a documented stable row height, a scroll container, and `aria-rowcount`/`aria-rowindex` guidance. |
| `DescriptionList` | ❌ | Key/value pairs — every detail pane. |
| `Stat` | ❌ | A number with a label and a trend. |
| `Timeline` | ❌ | Ordered events; `<ol>` semantics people get wrong. |
| `Accordion` | ❌ | `<details>`-equivalent with controlled state and correct `aria-expanded`. |
| `DiffView` | ⛔ as a component | A diff is a view of a domain object. Publish add/remove **surface tints** as tokens and stop. |
| `CodeBlock` | ❌ | `Code` is inline only. Block needs overflow, copy, optional line numbers. |
| `JsonView` | ❌ | Collapsible tree over JSON — every integration debugs with one. |
| `Monogram` | ✅ | |
| `AvatarGroup` | ❌ | Overflow count with an accessible summary. |
| `Chip` / `Badge` | ✅ | |
| `Progress` | ✅ | |
| `ProgressRing` | ❌ | Same ARIA, round. |
| `Sparkline` / charts | ⛔ | `json-render` covers this and a second mechanism would be a second thing to keep in step. |
| `Calendar` | ❌ | Date grid; the keyboard model is genuinely hard. Tier 2. |
| `Skeleton` | ✅ | |

### 2.4 Forms

Strongest area already.

| Component | State | Notes |
|---|---|---|
| `Field` | ✅ | Label, hint, error, `aria-describedby` wired via `cloneElement`. |
| `Input` `Textarea` `Select` `Checkbox` `Radio` `Switch` | ✅ | |
| `Form` | ❌ | Submit-on-enter, disabled-while-submitting, error summary focus. |
| `Fieldset` | ❌ | Grouped controls with a legend — required for radio groups. |
| `NumberInput` | ❌ | Step buttons, clamping, `inputMode`. |
| `SearchInput` | ❌ | `type="search"`, clear button, debounce, announced result count. |
| `PasswordInput` | ❌ | Reveal toggle with correct labelling. |
| `Combobox` | ❌ | **Hardest and most-wanted.** `aria-autocomplete`, active-descendant, announced option count. Nobody gets this right by hand. |
| `MultiSelect` / `TokenInput` | ❌ | Removable tokens, backspace semantics. |
| `DatePicker` / `TimePicker` | ❌ | Tier 2; text input with a parser gets most of the value. |
| `Slider` | ❌ | Keyboard stepping, `aria-valuetext`. |
| `FileDrop` | ❌ | Drag target that is also a keyboard-reachable button. |
| `ColorPicker` | ⛔ | Use `<input type="color">`. |
| `RichTextEditor` | ⛔ | Every author surface needs a different one. |
| `CodeEditor` | ⛔ | A dependency choice, not a design decision. |
| `Actions` | ✅ | |
| `CharacterCount` | ❌ | Live region that does not shout on every keystroke. |

### 2.5 Feedback, status, overlays

| Component | State | Notes |
|---|---|---|
| `Dialog` | ✅ | Focus trap, Escape, restore, backdrop drag. The floor at its best. |
| `ConfirmDialog` | ❌ | The destructive-action pattern, so it is consistent. |
| `Drawer` | ❌ | Side sheet; same trap machinery as `Dialog`. |
| `Popover` | ❌ | Anchored, dismiss-on-outside, `aria-expanded` on the trigger. |
| `Tooltip` | ✅ | |
| `Toast` / `useToast` | ✅ | Specced in §5 as `notify`, absent. Every extension has an "it saved" moment. |
| `Banner` / `Callout` | ✅ | Persistent page-level message with a tone and a dismiss. |
| `ErrorBoundary` | ✅ | §7's isolation table promises one. The iframe protects the *host's* shell; nothing protects the extension's own tree, so a throw is a blank frame — the worst-looking failure available and the one a partner demo hits. |
| `LiveRegion` / `useAnnounce` | ✅ | Currently hand-rolled with `ui-visually-hidden`. |
| `Empty` | ✅ | |
| `Spinner` | ✅ | |
| `Status` | ✅ | The `READY`/`FAILED` pill, matching the health contract. |
| `LoadingOverlay` | ❌ | Blocks a region while keeping layout, without a focus trap bug. |

### 2.6 Content and typography

| Component | State | Notes |
|---|---|---|
| `Heading` `Text` `Code` `Divider` | ✅ | |
| `Markdown` (block) | ❌ | Only `MarkdownInline` is published, and it is *designed* to destroy block structure for previews. Blocked on prose styling being in `theme.css`. |
| `Prose` | ❌ | The wrapper that styles arbitrary rendered markup. |
| `Kbd` | ❌ | Trivial, but everyone invents a different one. |
| `Truncate` | ❌ | Clamp plus a title, and the "is it actually truncated" check. |
| `CopyButton` | 🔧 inside `Mermaid.tsx` | Copy + confirmation + the clipboard-blocked path. |
| `Highlight` | ❌ | Search-match marking without `dangerouslySetInnerHTML`. |
| `MermaidBlock` | ✅ | Peer dependency. |

### 2.7 Primitives and hooks

Hooks are floor too: `useLocation` is the fix for the addressable-state
gap, and no component can supply it.

| Export | State | Notes |
|---|---|---|
| `MenuIcon` | ✅ | 30 named glyphs. |
| `VisuallyHidden` | ✅ (class only) | Needs a component. |
| `Portal` | ❌ | |
| `useFocusTrap` | 🔧 inside `Dialog` | Extract so `Drawer` and `Popover` share it. |
| `useAnnounce` | ✅ | |
| `useToast` | ✅ | |
| `useLocation` | ❌ | **Structural.** An extension's internal location is unaddressable, unsavable and unrestorable: `params` arrives once inside `hello`, there is no `params` host message, `allow-top-navigation` is correctly absent, and `localStorage` dies with the ephemeral port. Needs two additive envelope kinds. |
| `useTokens` | ✅ via `bridge.tokens` | |
| `useHotkeys` | ⛔ | §1 "not in v1". |
| `useClipboard` | ❌ | |

---

## 3. Order of work

**Tier 0 — done.** A second shape was impossible without these.

1. ✅ `styled.test.ts` coverage for everything new.
2. ✅ `AppShell` + `Scroll` + page-shell CSS — `html`/`body` height, `box-sizing` inherited from `.igni-root`, and the `min-height: 0` that makes a scroll container scroll instead of growing.
3. ✅ `ErrorBoundary`, `ToastHost`/`useToast`/`Banner`, `LiveRegion`/`useAnnounce`.
4. ✅ Selected state on `.ui-item`, driven by `[aria-selected]`/`[aria-current]` rather than a class — so what makes it look selected is the same thing that makes it announce as selected, and the two cannot disagree. Plus an inset edge, because a 16% tint is not a message on its own.
5. ✅ `params` host message + `setLocation`/`onParams`. Both additive, so neither is breaking under §8.
6. ✅ Table internals: cell padding, sticky opaque header, row-header weight, selected row, numeric columns with tabular figures, and `SortableTh`.

The reference extension consumes all of it: its live region, its error boundary and its sortable headers were on its own hand-rolled list and are not any more. That is the check working.

**Tier 1 — breadth**

`Split`, `Combobox`, `Popover`, `Drawer`, `Form`, `Fieldset`,
`SearchInput`, `NumberInput`, `DescriptionList`, `Accordion`,
`CodeBlock`, `Markdown`, `ConfirmDialog`, `StatusDot`, `Breadcrumbs`,
`NavList`, `Link`, `Kbd`, `CopyButton`.

**Tier 2 — later, additive, never breaking**

`Stepper`, `Pagination`, `Timeline`, `JsonView`, `MultiSelect`,
`Slider`, `FileDrop`, `DatePicker`, `Calendar`, `ProgressRing`,
`AvatarGroup`, `ContextMenu`, `CommandPalette`, `VirtualList` contract.

---

## 4. Restyling a deployment

### 4.1 One file, not a portal

A theme is `theme.json`, validated against a published JSON Schema
exactly as the manifest is, with an editor-facing `$schema` line.

It is a **file** because delivery is customer-run, air-gapped and
SI-led: a theme has to live in version control, be diffed in review,
and be promoted dev→prod with the rest of the deployment. A portal
that is the only place it exists can do none of that.

```jsonc
{
  "$schema": "https://igni.sh/schema/theme-1.json",
  "name": "Northwind",
  "extends": "igni-dark",            // start from a shipped base

  "tokens": {
    "light": { "--bg": "#ffffff", "--accent-blue": "#0b5cab" },
    "dark":  { "--bg": "#14161a" }
  },

  "icons": {                          // path data only — see 4.3
    "lock": { "viewBox": "0 0 16 16", "d": "M4 7V5a4 4 0 0 1 8 0v2" }
  },

  "brand": {
    "wordmark": "assets/northwind.svg",
    "appName": "Northwind Code"
  }
}
```

Precedence: shipped base → deployment theme → group policy pack
override. Group-scoped, because a deployment already resolves policy
per group and a theme is policy.

How it reaches a frame: tokens are already pushed over the bridge at
`hello` and reapplied on change. Nothing new is needed for extensions
to follow a theme — that mechanism exists and works.

### 4.2 Does it need a preview?

**Yes, and the justification is not that it looks nice.**

- **Contrast cannot be read off a token list.** `--danger` on
  `--bg-surface` either clears AA or does not, and nobody knows by
  looking. We have already shipped this bug: `--fg-faint` and
  `--fg-muted` were used interchangeably for empty states until an
  audit found one of them failing.
- **Tokens compose.** 50 tokens across 40 components is not something
  a person simulates in their head.
- **The person choosing brand colours does not read CSS.**

So the portal page is a **reader of the doc**, showing every component
in every state against the candidate theme, with an automated check
over every foreground×surface pair that fails the build on a contrast
regression. It can *export* a `theme.json` so a brand person can
fiddle and hand the file to the SI — but the deployment loads the
file, and pushing it is a deliberate act.

That contrast check should also run in CI against our own themes.

### 4.3 Icons

Three different questions:

| Want | Answer |
|---|---|
| A different glyph for an existing named icon | **Yes**, via `icons` above. The 30 published names stay; only the drawing changes. Safe, because the name set and its meanings remain ours. |
| A brand-new icon | The extension bundles its own SVG inside its own frame. Not a theme concern. |
| A different logo / product name | **Yes**, via `brand`. Different thing from icons, same file. |

**Path data only. Never raw SVG markup.** We accept `viewBox` and `d`
and render them inside our own `<svg>` with our own `stroke`,
`fill: none` and `currentColor`. Accepting a pasted `<svg>` accepts
`<script>`, `<foreignObject>`, `xlink:href` and external references —
a theme file would become an XSS vector into the host, and a theme
file is precisely the artifact that gets emailed between an SI and a
customer. The schema should enforce the allowed path grammar, and the
renderer should treat anything else as a missing icon rather than
sanitise-and-hope.

Overriding an icon's *meaning* is not offered. `lock` must keep
meaning locked, or every screenshot, document and support answer
stops being true for that customer.

### 4.4 Should a customer fork instead?

No, and this is the question the whole module contract exists to
answer. A fork ends the upgrade path: that customer is permanently on
the version they forked at, and every security fix becomes a merge.
Theming through a declarative file keeps rebranding on the supported
path, which is also what makes it sellable — "your colours, your
wordmark, our release cadence".

The line is: **appearance is configuration, behaviour is not.** A
theme may change colour, spacing, radius, type, icon drawings and
brand assets. It may not reorder navigation, hide features, or change
what a control does — those are policy, and policy packs already
exist.
