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
| **Migration** | 86 feature classes joined a primitive. 137 declarations deduplicated. **Nothing lost a property** — verified by expanding the primitive into each joiner and comparing resolved declarations. |
| **Published** | All scales and primitives are in `clients/ui/` for extensions, held to the app's by three tests. |

Coverage: **1437 of 1789** spacing, type and shape declarations name a scale, from zero.

## Needs a human eye

42 classes **gained** a property they were missing — that is the point of the primitive, and it is also the only place pixels moved. Nothing regressed mechanically; whether each looks right is a judgement.

The 34 Playwright visual baselines are Linux-only and cannot run on this machine, so this list is what a CI round-trip should be checked against. Expect some baselines to need regenerating with `--update-snapshots` — a deliberate approval, not a rubber stamp.

### `align-items` — 29 classes
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
- [ ] `.msg-assistant-interrupted-actions`
- [ ] `.msg-user-actions`
- [ ] `.notify-row`
- [ ] `.page-toolbar`
- [ ] `.plugin-detail-actions`
- [ ] `.plugins-actions`
- [ ] `.plugins-toolbar`
- [ ] `.prov-row`
- [ ] `.update-prompt-actions`
- [ ] `.watcher-row`
- [ ] `.welcome-setup-actions`

### `gap` — 15 classes
- [ ] `.chat-row`
- [ ] `.demo-page-composer-actions`
- [ ] `.dir-list`
- [ ] `.doc-locked-actions`
- [ ] `.file-ref-list`
- [ ] `.hitl-arg-row`
- [ ] `.kb-list`
- [ ] `.kb-pick-list`
- [ ] `.kb-stale-row`
- [ ] `.mcp-head-actions`
- [ ] `.notify-row`
- [ ] `.overflow-list`
- [ ] `.page-toolbar`
- [ ] `.prov-row`
- [ ] `.sidebar-list`

### `display` — 7 classes
- [ ] `.dir-list`
- [ ] `.file-ref-list`
- [ ] `.kb-pick-list`
- [ ] `.kb-stale-row`
- [ ] `.notify-row`
- [ ] `.page-toolbar`
- [ ] `.sidebar-list`

### `font-size` — 6 classes
- [ ] `.afs-empty`
- [ ] `.codeindex-empty`
- [ ] `.ctx-empty`
- [ ] `.kb-empty`
- [ ] `.plugins-empty`
- [ ] `.watcher-log-empty`

### `flex-direction` — 5 classes
- [ ] `.dir-list`
- [ ] `.file-ref-list`
- [ ] `.kb-pick-list`
- [ ] `.sidebar-list`
- [ ] `.workflow-phase-list`

### `color` — 2 classes
- [ ] `.afs-empty`
- [ ] `.codeindex-empty`

## Not done

**Padding is 62% on a scale and the rest is probably correct.** What stayed literal is overwhelmingly odd — 1, 3, 5, 7, 9, 11, 13px — which is optical correction against a 1px border, not drift. Forcing it onto even steps would move things a pixel for nothing.

**145 feature classes have not joined a primitive.** Mostly `-card`, `-item`, `-badge` and `-title`, where the existing uses disagree too much for a single value to be obviously right. `-title` will never join: its variation is a heading level, not drift.

**The app does not use the primitives in markup.** Feature classes are grouped into the primitive's selector in CSS, which deduplicates without touching 64 `.tsx` files. Moving markup to `class="ui-card"` is the tidier end state and is not required for extensions, which is what this was for.

**No component library for the iframe path.** Extensions get CSS classes and tokens, not React or web components. Worth revisiting once two real extensions exist and we know what they reimplemented.
