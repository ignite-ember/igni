/**
 * UI sandbox for the interrupted-assistant banner.
 *
 * Reachable at ``?demo=interrupted`` (see ``main.tsx``). Renders the
 * banner in every meaningful state — cancelled / errored / abandoned
 * — alongside the partial assistant content above it and a normally-
 * completed bubble as a control. No real BE / WS / agent needed;
 * every state is canned so visual iteration on the banner (color,
 * spacing, button weight, keyboard hints) is fast and deterministic.
 *
 * What this pins:
 *
 *   • The banner mounts BELOW the partial content, never replaces
 *     it — the user must see what was streamed so they can decide
 *     between retry / discard / edit-prompt.
 *   • The reason drives both the label text ("Run cancelled" /
 *     "Run stopped" / "Run interrupted") and the left-border accent
 *     color. Errored banners surface the actual ``last_error`` so
 *     the user knows whether to retry vs call support.
 *   • Three actions on every banner: Retry (primary, blue) /
 *     Edit prompt (neutral) / Discard (danger, red). Each has a
 *     keyboard shortcut chip: ↵ / E / ⌫.
 *   • The completed bubble at the bottom shows what a normal
 *     assistant turn looks like WITHOUT a banner — the contrast
 *     makes it obvious why a hidden partial is bad UX.
 *
 * Click + keydown events are logged to the on-page console pane
 * below so you can confirm the callbacks fire.
 */

import { useCallback, useState } from "react";
import { AssistantMessage } from "../components/ChatItems";
import type { ChatItem } from "../chat/model";
import type { AssistantInterrupted } from "../chat/model";

interface LogEntry {
  ts: number;
  label: string;
}

const PARTIAL_1 = `Sure — let me think about that. There are basically three trade-offs you should consider:

1. **Latency.** If you ship a synchronous \`fetch_url\` call inside the agent loop, every upstream timeout blocks the whole turn.

2. **Cost.** Every tool call is a separate model-side round trip with its own prompt-cache miss, so the token bill scales with tool surface, not just input length.

3. **Idempotency.** If the model retries a half-applied edit, you need`;

const PARTIAL_2 = `Let me pull that up — give me a second. Looking at the arxiv export for Q2 2024, the top cited paper in retrieval-augmented generation is \`Self-RAG\` with 412 citations, followed by`;

const PARTIAL_3 = `I'll outline the design doc.

- **Section 1: goals** — surface interrupted runs durably so the FE has a recovery surface on next render.
- **Section 2: storage** — extend the existing pending-message table with \`interrupted_at\`, \`interrupted_reason\`,`;

const COMPLETED = `Done — I wrapped the helper into \`PendingMessageJournal.mark_interrupted()\` so the cancel + error paths in \`RunController\` can stamp the row without reaching into the store directly.

The wire schema mirrors the storage shape (see \`InterruptedRun.from_interrupted_row\`), and the schema migration is idempotent so a re-running BE never crashes.`;

interface Scenario {
  key: string;
  title: string;
  subtitle: string;
  reason: AssistantInterrupted | undefined;
  partial: string;
  /** Extra text shown in the errored-variant label. */
  errorDetail?: string;
}

const SCENARIOS: Scenario[] = [
  {
    key: "cancelled",
    title: "User hit Esc mid-stream",
    subtitle:
      "Banner reason: cancelled. Left-border accent: ember-orange. " +
      "FE-side stamp via markAssistantInterrupted('cancelled'); " +
      "BE-side stamp via run_controller.cancel_run.",
    reason: "cancelled",
    partial: PARTIAL_1,
  },
  {
    key: "errored",
    title: "Agent raised mid-run",
    subtitle:
      "Banner reason: errored. Left-border accent: ember-red. The " +
      "error string is shown inline so the user can tell apart " +
      '"I stopped it" from "it crashed".',
    reason: "errored",
    partial: PARTIAL_2,
    errorDetail: "anthropic 504: model timeout",
  },
  {
    key: "abandoned",
    title: "BE shut down or lost connection",
    subtitle:
      "Banner reason: abandoned. Left-border accent: warning. " +
      "Rarer than cancelled/errored — fires on reconnect after a " +
      "BE crash that didn't surface a clean terminal event.",
    reason: "abandoned",
    partial: PARTIAL_3,
  },
  {
    key: "completed",
    title: "Normally-completed bubble (control)",
    subtitle:
      "No banner mounts on a clean run — the contrast against the " +
      "three interrupted states above is what makes the missing-partial " +
      "behaviour obvious.",
    reason: undefined,
    partial: COMPLETED,
  },
];

/** Build a ChatItem with the right shape for the assistant render path. */
function assistantItem(
  id: number,
  text: string,
  reason?: AssistantInterrupted,
): Extract<ChatItem, { kind: "assistant" }> {
  return reason
    ? { kind: "assistant", id, text, interrupted: reason }
    : { kind: "assistant", id, text };
}

function App() {
  const [log, setLog] = useState<LogEntry[]>([]);
  /** Track which (scenario-key, item-id) the user picked so the
   *  faux-composer below can show the seed text they'd see in
   *  the real app. The seed text comes from each scenario's
   *  ``partial`` (the original prompt that triggered the run). */
  const [seed, setSeed] = useState<{ scenarioKey: string; text: string } | null>(null);

  const appendLog = useCallback((label: string) => {
    setLog((prev) => [...prev.slice(-19), { ts: Date.now(), label }]);
  }, []);

  const onRetry = useCallback(
    (assistantItemId: number) => {
      appendLog(`Retry → assistant item #${assistantItemId}`);
    },
    [appendLog],
  );
  const onDiscard = useCallback(
    (assistantItemId: number) => {
      appendLog(`Discard → assistant item #${assistantItemId}`);
    },
    [appendLog],
  );
  const onEditPrompt = useCallback(
    (scenarioKey: string, text: string) => (assistantItemId: number) => {
      appendLog(`Edit prompt → assistant item #${assistantItemId} → composer seeded`);
      setSeed({ scenarioKey, text });
    },
    [appendLog],
  );

  return (
    <div className="demo-page">
      <header className="demo-page-header">
        <h1>Interrupted-assistant banner</h1>
        <p className="demo-page-lede">
          Surfaces a partial assistant bubble with three recovery
          actions instead of a hidden "N messages interrupted"
          notice. Mounts when an agent run stops mid-stream — Esc,
          Stop, error, or BE crash. Same UX across the three
          reasons (cancelled / errored / abandoned), each tinted
          with a left-border accent.
        </p>
        <p className="demo-page-lede">
          Click any button or focus the banner and press{" "}
          <kbd>Enter</kbd>, <kbd>Backspace</kbd>, or <kbd>e</kbd>{" "}
          to test the keyboard shortcuts. Events appear in the
          console at the bottom.
        </p>
      </header>

      <div className="demo-page-grid">
        {SCENARIOS.map((s) => (
          <section className="demo-page-panel" key={s.key}>
            <h2 className="demo-page-panel-title">{s.title}</h2>
            <p className="demo-page-panel-subtitle">{s.subtitle}</p>
            <div className="demo-page-panel-body">
              <AssistantMessage
                item={assistantItem(1, s.partial, s.reason)}
                onRetry={s.reason ? onRetry : undefined}
                onDiscard={s.reason ? onDiscard : undefined}
                onEditPrompt={s.reason ? onEditPrompt(s.key, s.partial) : undefined}
              />
              {s.errorDetail && (
                <p className="demo-page-error-detail">
                  Banner label should include:{" "}
                  <code>{s.errorDetail}</code>
                </p>
              )}
            </div>
          </section>
        ))}
      </div>

      {/* Faux Composer — mirrors what the real app does when
          ``setComposerSeed({text, n})`` fires. Shows the seeded
          text in a read-only editor area + a "Send" button so
          the user can see the full UX chain:
            1. Click Edit prompt on a banner
            2. Composer below fills with the original prompt
            3. User edits (typing would be live in the real app)
            4. User clicks Send (or hits Enter) to re-fire with
               the edited text.
          The real app uses the actual ``Composer`` component;
          this is a faithful stub so the demo doesn't need a
          EmberClient or BE connection. */}
      <section className="demo-page-composer">
        <h3>Composer (faux)</h3>
        <p className="demo-page-composer-subtitle">
          In the real app, the Composer at the bottom of the chat
          fills with this text when Edit prompt is clicked. The
          user edits and re-sends to run the agent with the
          tweaked prompt.
        </p>
        <div className="demo-page-composer-edit">
          {seed ? (
            <>
              <span className="demo-page-composer-label">
                Seeded from <code>{seed.scenarioKey}</code>:
              </span>
              <pre className="demo-page-composer-text">{seed.text}</pre>
            </>
          ) : (
            <span className="demo-page-composer-empty">
              No seed yet — click "Edit prompt" on any banner above.
            </span>
          )}
        </div>
        <div className="demo-page-composer-actions">
          <button
            type="button"
            className="demo-page-composer-send"
            disabled={!seed}
          >
            Send (re-fire)
          </button>
        </div>
      </section>

      <section className="demo-page-console">
        <h3>Console</h3>
        {log.length === 0 ? (
          <p className="demo-page-console-empty">
            No events yet — click a button or press a shortcut.
          </p>
        ) : (
          <ul>
            {log.map((entry) => (
              <li key={entry.ts}>
                <span className="demo-page-console-ts">
                  {new Date(entry.ts).toLocaleTimeString()}
                </span>{" "}
                {entry.label}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

// NOTE: an earlier version of this file had a standalone
// ``createRoot`` mount at module level (intended for opening the
// demo directly without main.tsx). It raced main.tsx's own
// createRoot on the same ``#root`` container and produced blank
// renders in the Tauri webview. React 18 logs a "container
// already used" warning when the second createRoot fires and the
// resulting root tree ends up stale in some webview versions.
// The standalone mount is removed; the demo is now exclusively
// mounted via main.tsx's ``pickRoot()``.

export { App as InterruptedBannerDemo };
