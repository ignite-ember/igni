/**
 * The desktop app's main screens, as a list two specs share.
 *
 * E5. The portal got sixty-four pictures and then a baseline per page
 * (E2–E4); this is the same treatment for the app, and the same reason
 * — 727 unit tests and 48 browser tests, and not one of them had ever
 * looked at a screen.
 *
 * Every entry is driven by a `?demo=` route, so the data is fixed
 * fixtures rather than a backend: no session ids, no timestamps, no
 * model output that differs between runs. That is what makes a picture
 * comparable tomorrow.
 *
 * The team scenarios are chosen rather than enumerated. `SCENARIOS` in
 * `dev/OrchestrateDemo.tsx` has fifteen, several of which differ by one
 * event in a card — a baseline each buys little and costs a PNG each.
 * These are the surfaces a change would be felt on.
 */

export type AppScreen = {
  /** File-name stem for shots and baselines. */
  name: string
  /** Query string to open. */
  demo: string
  /**
   * Scenario title to click first, for the `team` sandbox, which mounts
   * with a nav and nothing selected.
   */
  scenario?: RegExp
  /** Extra settle time where a screen animates itself in. */
  settleMs?: number
  /**
   * Expand every collapsed tool card first.
   *
   * Tool cards mount collapsed, showing a truncated argument preview.
   * On the screens whose subject *is* what the card contains — the diff
   * table, a long thinking block — a shot of the collapsed row pictures
   * the one thing it was not taken for.
   */
  expandToolCards?: boolean
  /**
   * A control to click before capturing.
   *
   * Two sandboxes mount idle — "No items yet. Click Stream to start."
   * — so a shot taken on arrival pictures an empty box where the
   * subject should be. Both were captured that way on the first pass.
   */
  click?: RegExp
  /**
   * What has to be on the page before the shot is taken.
   *
   * A fixed `settleMs` is a guess about how long a machine takes, and
   * under x86_64 emulation two screens failed their own baselines once
   * in six runs — then passed five times, which is the worst evidence
   * there is. Waiting for the thing itself removes the class.
   */
  ready?: { selector: string; count?: number }
}

export const APP_SCREENS: readonly AppScreen[] = [
  // The chat surface, in the states that exercise its renderers.
  {
    name: '01-conversation',
    demo: 'team',
    scenario: /Team — inside a real conversation/i,
    ready: { selector: '.orchestrate-head' },
  },
  { name: '02-kitchen-sink', demo: 'team', scenario: /Kitchen sink/i },
  {
    name: '03-markdown',
    demo: 'team',
    scenario: /Markdown rendering/i,
    // mermaid is a dynamic import of ~700KB rendering ten diagrams.
    // Waiting for all ten, not for 2.5 seconds: this is the screen
    // that flaked, and D7 established the count.
    ready: { selector: 'svg[aria-roledescription], svg[id^="mermaid"]', count: 10 },
    settleMs: 1_200,
  },
  {
    name: '04-edit-tools',
    demo: 'team',
    scenario: /Edit tools/i,
    expandToolCards: true,
    ready: { selector: '.diff-table', count: 3 },
  },
  {
    name: '05-tool-states',
    demo: 'team',
    scenario: /Tool states/i,
    expandToolCards: true,
  },
  { name: '06-long-thinking', demo: 'team', scenario: /Long thinking block/i },
  { name: '07-wall-of-tools', demo: 'team', scenario: /30 tool calls/i },

  // Orchestration.
  {
    name: '08-broadcast',
    demo: 'team',
    scenario: /Broadcast — three specialists/i,
  },
  {
    name: '09-broadcast-error',
    demo: 'team',
    scenario: /one specialist errored/i,
  },
  { name: '10-single-agent', demo: 'team', scenario: /Single agent/i },
  {
    name: '11-paused-for-approval',
    demo: 'team',
    scenario: /Sub-agent paused waiting/i,
  },

  // Human-in-the-loop.
  { name: '12-hitl-single', demo: 'team', scenario: /HITL — one approval/i },
  { name: '13-hitl-batched', demo: 'team', scenario: /three approvals batched/i },

  // Everything with its own sandbox.
  { name: '14-plan-mode', demo: 'plan' },
  // Straight to the finished run: the streaming variants are a
  // different shot on every frame and cannot be compared.
  {
    name: '15-workflow',
    demo: 'workflow',
    click: /Jump to success/i,
    ready: { selector: '[data-status="done"]' },
  },
  { name: '16-interrupted', demo: 'interrupted' },
  {
    name: '17-visualizer',
    demo: 'viz-stream',
    click: /One-shot/i,
    ready: { selector: '[data-status="done"]' },
  },
]
