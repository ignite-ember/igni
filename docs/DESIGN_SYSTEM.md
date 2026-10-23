# Design system — status

The UI refactor, as it stands. Updated as it moves.

## Why this exists

igni had 84 colour tokens and nothing else. `gap` had 20 distinct values, `padding` 161, `font-size` 27, `border-radius` 28 — every one hardcoded — so each feature invented its own numbers and none of them agreed.

The 231 classes named `-row`, `-card`, `-actions` looked like duplication and were not: `*-row` agreed only on `display: flex`, and deduplicating all fourteen patterns saved 84 declarations out of 17,789 lines. They did not repeat. They diverged, because there was nothing to diverge *from*.

## Done

| | |
|---|---|
| **Spacing scale** | `--space-1`…`--space-16`. 375 declarations migrated. Verified byte-identical by resolving every token back and diffing. |
| **Type scale** | `--text-2xs`…`--text-4xl`. 22 hardcoded sizes → 9 steps. 267 moved, none by more than 1px. 98% of font sizes name a step. |
| **Shape** | `--radius-pill` and `--radius-circle` added. `9999px` and `999px` unified. |
| **Primitives** | 9: `ui-row`, `ui-actions`, `ui-list`, `ui-toolbar`, `ui-card`, `ui-item`, `ui-badge`, `ui-empty`, `ui-table`. Decided, not extracted. |
| **Empty-state colour** | Was split 8/8 between `--fg-faint` and `--fg-muted`. Both clear WCAG AA, so decided on meaning: an empty state is content. All 17 agree. |
| **Migration** | 105 feature classes joined a primitive; 203 declarations deduplicated. **Zero properties overridden or lost**, verified by expanding every primitive into every joiner and comparing resolved declaration sets against the pre-migration file. |
| **Cascade order** | The primitives sit above every feature rule, so a feature always wins. They did not at first, and five declarations were silently overridden — see below. |
| **Published** | All scales and primitives are in `clients/ui/` for extensions, held to the app's by three tests. |

Coverage: **1437 of 1789** spacing, type and shape declarations name a scale, from zero.

## Needs a human eye

86 classes **gained** a property they were missing. That is what a primitive is for, and it is the only place pixels moved — verified: zero properties were overridden or lost.

The 34 Playwright visual baselines are Linux-only and cannot run on this machine, so this is the list a CI round-trip should be checked against. Expect some baselines to need regenerating with `--update-snapshots` — an approval, not a rubber stamp.

### `align-items` — 31 classes

- [ ] `.afs-row`
- [ ] `.chat-row`
- [ ] `.codeindex-actions`
- [ ] `.codeindex-timeline-row`
- [ ] `.codeindex-toolbar`
- [ ] `.comment-review-actions`
- [ ] `.demo-page-composer-actions`
- [ ] `.dialog-actions`
- [ ] `.doc-locked-actions`
- [ ] `.drawer-toolbar`
- [ ] `.group-conflict-actions`
- [ ] `.hitl-arg-row`
- [ ] `.kb-drift-banner-actions`
- [ ] `.kb-empty-actions`
- [ ] `.kb-share-actions`
- [ ] `.kb-share-link-row`
- [ ] `.kb-stale-row`
- [ ] `.mcp-head-actions`
- [ ] `.mode-badge`
- [ ] `.msg-assistant-interrupted-actions`
- [ ] `.msg-user-actions`
- [ ] `.notify-row`
- [ ] `.page-toolbar`
- [ ] `.plugin-detail-actions`
- [ ] `.plugins-actions`
- [ ] `.plugins-toolbar`
- [ ] `.prov-row`
- [ ] `.tool-agent-badge`
- [ ] `.update-prompt-actions`
- [ ] `.watcher-row`
- [ ] `.welcome-setup-actions`

### `gap` — 21 classes

- [ ] `.chat-row`
- [ ] `.compact-card-badge`
- [ ] `.demo-page-composer-actions`
- [ ] `.dir-list`
- [ ] `.doc-locked-actions`
- [ ] `.file-ref-list`
- [ ] `.hitl-arg-row`
- [ ] `.kb-list`
- [ ] `.kb-pick-list`
- [ ] `.kb-stale-row`
- [ ] `.mcp-head-actions`
- [ ] `.mode-badge`
- [ ] `.notify-row`
- [ ] `.overflow-list`
- [ ] `.page-toolbar`
- [ ] `.plan-card-badge`
- [ ] `.plugins-card`
- [ ] `.prov-row`
- [ ] `.sidebar-list`
- [ ] `.tool-agent-badge`
- [ ] `.tool-card`

### `display` — 11 classes

- [ ] `.dir-list`
- [ ] `.file-ref-list`
- [ ] `.kb-pick-list`
- [ ] `.kb-stale-row`
- [ ] `.mode-badge`
- [ ] `.notify-row`
- [ ] `.page-toolbar`
- [ ] `.plugins-card`
- [ ] `.sidebar-list`
- [ ] `.tool-agent-badge`
- [ ] `.tool-card`

### `flex-direction` — 8 classes

- [ ] `.attach-card`
- [ ] `.dir-list`
- [ ] `.file-ref-list`
- [ ] `.kb-pick-list`
- [ ] `.plugins-card`
- [ ] `.sidebar-list`
- [ ] `.tool-card`
- [ ] `.workflow-phase-list`

### `font-size` — 6 classes

- [ ] `.afs-empty`
- [ ] `.codeindex-empty`
- [ ] `.ctx-empty`
- [ ] `.kb-empty`
- [ ] `.plugins-empty`
- [ ] `.watcher-log-empty`

### `color` — 3 classes

- [ ] `.afs-empty`
- [ ] `.codeindex-empty`
- [ ] `.comment-review-item`

### `padding` — 3 classes

- [ ] `.compact-card-badge`
- [ ] `.plan-card-badge`
- [ ] `.tool-card`

### `text-align` — 2 classes

- [ ] `.comment-review-item`
- [ ] `.file-ref-item`

### `cursor` — 1 classes

- [ ] `.comment-review-item`

## Not done

**Padding is 62% on a scale and the rest is probably correct.** What stayed literal is overwhelmingly odd — 1, 3, 5, 7, 9, 11, 13px — which is optical correction against a 1px border, not drift. Forcing it onto even steps would move things a pixel for nothing.

**Four classes are named like a primitive and are not one**, so they were left alone rather than forced to look like one:

| | |
|---|---|
| `.hitl-card`, `.wall-card` | no `background`, no `border` — layout wrappers with a misleading name |
| `.mdi-item` | no `padding` |
| `.kb-tree-badge` | no `border-radius`, no `font-weight` |

Joining them would make a transparent wrapper grow a surface. The rule used throughout: a class may only join a primitive if it already declares that primitive's **defining** properties.

**`-title` will never join.** Its variation spans four type steps, which is a heading level rather than drift.

**The app does not use the primitives in markup.** Feature classes are grouped into the primitive's selector in CSS, which deduplicates without touching 64 `.tsx` files. Moving markup to `class="ui-card"` is the tidier end state and is not required for extensions, which is what this was for.

**A React package exists**, at `clients/web/src/ui-react/`, imported as `@igni/ui-react`.

Its contents were measured, not guessed. CodeIndex imports nothing from the app but a type. Artifacts is 7,737 lines and imports exactly seven components — `MenuIcon`, `Tooltip`, `MarkdownInline`, `Monogram`, `RichFences`, `Skeleton`, `OverflowMenu`. Those moved into the package and the app now imports them from it, so there is one copy.

Seven was never the whole floor, though. Artifacts also writes `className="btn btn-primary"` by hand, and so would every extension. `primitives.tsx` closes that with thirteen typed wrappers over the classes the app already uses — `Button`, `Input`, `Chip`, `Badge`, `Row`, `Actions`, `List`, `Toolbar`, `Card`, `Item`, `Table`, `Empty`, `Dialog`.

They hold no state and make no decisions; appearance stays in CSS where the app and the extension share it. `Dialog` is the exception, because a dialog is behaviour: focus trap, Escape, focus restored to whatever opened it, and a drag that starts inside not counting as a backdrop click. Those are the parts an extension would reimplement and get wrong in ways only keyboard and screen-reader users notice.

**React rather than web components**, deliberately. Six of the seven are worse in a shadow root: `MarkdownInline` and `RichFences` are a react-markdown pipeline with remark and rehype plugins, `Tooltip` and `OverflowMenu` are positioning that wants the light DOM. And an extension runs in its own frame bundling its own React, so a React package costs it nothing it was not already paying. Web components only become right if igni's own UI moves too — a far larger project, and one that would put artifacts' passage selection at risk, since selection across shadow boundaries is hard.

**Not yet publishable.** There is no npm packaging or publish pipeline; `@igni/ui-react` is a tsconfig and Vite alias. Extraction is a build change, not a code change.

## A bug this refactor introduced and fixed

The first migration put the primitives block where the old `.btn` rule was — mid-file. CSS resolves equal specificity by order, so every feature rule *above* that point lost to the primitive instead of winning.

Five declarations were silently overridden: `.msg-user-actions` and `.msg-assistant-interrupted-actions` lost their own gaps, `.msg-assistant-interrupted-actions` went from `inline-flex` to `flex`, and `.plan-card-actions` and `.welcome-setup-actions` had theirs widened.

It was caught by comparing resolved declaration sets against the pre-migration file rather than by reading the diff, which is the only way a cascade bug shows up. The block now sits above every feature rule, and the check is part of the migration.
