/**
 * UI sandbox for the plan-mode UI (row 50).
 *
 * Reachable at ``?demo=plan`` (see main.tsx). Renders every state
 * of the plan-mode surface — the status-line badge, the inline
 * "Agent entered plan mode" info banner, and the PlanCard in all
 * three states (pending / approved / dismissed) — driven by local
 * state so you can click Approve / Refine and watch the
 * transitions without spinning up a real BE.
 *
 * Editing this file: keep the scenarios self-contained — the
 * demo is the spec for what the UI should look like at each
 * stage, so it doubles as a visual regression target.
 */

import { useState } from "react";
import { ChatItemView } from "../components/ChatItems";
import { CtxMeter, PlanBadge, SessionChip } from "../components/StatusBits";
import { planItem, type ChatItem, type PlanTask } from "../chat/model";

const SAMPLE_PLAN_MARKDOWN = `## JWT Auth Refactor — Implementation Plan

### Phase 1 — Backend: JWT Core
1. **\`src/ember_code/core/auth/jwt_.py\`** *(new)* — add \`generate_access_token\`, \`verify_token\`, \`generate_refresh_token\`, \`TokenPayload\` dataclass.
2. **\`src/ember_code/core/auth/blacklist.py\`** *(new)* — file-backed revocation set with \`revoke_token\` / \`is_revoked\`.
3. **\`src/ember_code/core/auth/credentials.py\`** *(extend)* — replace static helpers with an \`AuthStore\` class that also tracks \`refresh_token\`.

### Phase 2 — Middleware
4. **\`src/ember_code/backend/auth_middleware.py\`** *(new)* — \`AuthMiddleware\` wraps the request handler; extracts \`Authorization: Bearer\`, validates, attaches claims to context.
5. **\`src/ember_code/transport/websocket.py\`** *(update)* — call the middleware on connect; reject the handshake on failure.

### Phase 3 — Frontend
6. **\`clients/web/src/lib/authStore.ts\`** *(new)* — module-level token state (no \`localStorage\` — XSS safe).
7. **\`clients/web/src/components/panels/LoginPanel.tsx\`** *(refactor)* — show org/user from JWT claims; explicit logout button.

### Risk note
The migration is **additive** — existing \`credentials.json\` users aren't forced to re-authenticate. Safe to ship incrementally; old + new auth paths coexist for 2 releases before the file fallback is dropped.

### Test plan
- Unit: signature roundtrip, expiry, blacklist hit/miss (\`tests/test_jwt.py\`).
- Integration: WS handshake with valid / expired / blacklisted tokens.
- Manual: existing sessions stay logged in after upgrade; \`/logout\` revokes and forces re-auth.
`;

type CardState = "pending" | "approved" | "dismissed";

const SAMPLE_TASKS: PlanTask[] = [
  { content: "Generate JWT signing keys", status: "pending", activeForm: "Generating JWT signing keys" },
  { content: "Add /auth/refresh endpoint", status: "pending", activeForm: "Adding /auth/refresh endpoint" },
  { content: "Migrate session table", status: "pending", activeForm: "Migrating session table" },
  { content: "Wire WS middleware", status: "pending", activeForm: "Wiring WS middleware" },
  { content: "Refactor LoginPanel", status: "pending", activeForm: "Refactoring LoginPanel" },
];

function makePlan(
  state: CardState,
  tasks: PlanTask[] = SAMPLE_TASKS,
): Extract<ChatItem, { kind: "plan" }> {
  const base = planItem(SAMPLE_PLAN_MARKDOWN, tasks);
  if (base.kind !== "plan") throw new Error("planItem produced wrong kind");
  return { ...base, state };
}

/** Mid-execution snapshot — first task done, second in flight,
 *  rest pending. Demos the live checklist state the user sees
 *  while the agent executes. */
const TASKS_IN_FLIGHT: PlanTask[] = [
  { ...SAMPLE_TASKS[0], status: "completed" },
  { ...SAMPLE_TASKS[1], status: "in_progress" },
  SAMPLE_TASKS[2],
  SAMPLE_TASKS[3],
  SAMPLE_TASKS[4],
];

/** Fully-executed snapshot — all tasks done. */
const TASKS_DONE: PlanTask[] = SAMPLE_TASKS.map((t) => ({ ...t, status: "completed" as const }));

export function PlanModeDemo() {
  // The "live" card (top of the chat) — Approve / Refine actually
  // mutates this so you can click through the transitions.
  const [liveCard, setLiveCard] = useState(makePlan("pending"));
  // The "permission mode" toggle drives the badge. Approve in the
  // live card flips it to "default"; the toggle button at the top
  // resets it.
  const [mode, setMode] = useState<string>("plan");

  const onApprove = (id: number) => {
    setLiveCard((prev) => (prev.id === id ? { ...prev, state: "approved" } : prev));
    setMode("default");
  };
  const onReject = (id: number) => {
    setLiveCard((prev) => (prev.id === id ? { ...prev, state: "dismissed" } : prev));
  };

  // Static cards for the gallery — each one demos one terminal
  // state without depending on click flow. Always rendered.
  const approvedCard = makePlan("approved");
  const dismissedCard = makePlan("dismissed");
  // Mid-execution + fully-executed checklist states (after the
  // user approved and the agent is now calling ``todo_write``
  // to tick tasks off).
  const inFlightCard = makePlan("approved", TASKS_IN_FLIGHT);
  const doneCard = makePlan("approved", TASKS_DONE);

  // Mock info banner the same shape App.tsx renders when the
  // agent enters plan mode itself.
  const infoText = "Agent entered plan mode — multi-file refactor: auth subsystem spans 4 services.";

  // ``body`` is locked with ``overflow: hidden`` (the real chat
  // virtualizer owns scrolling) — for this static demo we set up
  // our own scroll container so all five sections are reachable.
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        background: "var(--bg)",
      }}
    >
      <header className="brand-band" style={{ flexShrink: 0 }}>
        <div className="brand-band-inner">
          <span className="brand-name">igni · plan-mode demo</span>
        </div>
      </header>

      <div
        className="statusline"
        style={{ marginTop: 0, paddingBottom: 8, flexShrink: 0 }}
      >
        <SessionChip sessionId="demoabcd1234" />
        <PlanBadge mode={mode} />
        <CtxMeter tokens={12340} max={200000} pct={6.17} />
        <button
          type="button"
          className="brand-update"
          style={{ marginLeft: "auto" }}
          onClick={() => {
            setLiveCard(makePlan("pending"));
            setMode("plan");
          }}
          title="Reset the live card and the mode badge."
        >
          Reset
        </button>
      </div>

      <main
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          padding: "12px 18px 60px",
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <section className="demo-section">
          <h3 className="demo-section-title">1. Status-line badge (top of window)</h3>
          <p className="demo-section-blurb">
            Pulsing orange chip — only visible when{" "}
            <code>status.permission_mode === "plan"</code>. Hover shows
            the explanation tooltip. After you click <strong>Approve</strong>{" "}
            below, the badge disappears (mode flips to{" "}
            <code>default</code>).
          </p>
        </section>

        <section className="demo-section">
          <h3 className="demo-section-title">
            2. Info banner — when the AGENT entered plan mode
          </h3>
          <p className="demo-section-blurb">
            Injected as an inline info ChatItem when{" "}
            <code>permission_mode_changed</code> arrives with{" "}
            <code>source: "agent"</code>. Tells the user{" "}
            <em>why</em> the mode changed.
          </p>
          <div className="msg-info">{infoText}</div>
        </section>

        <section className="demo-section">
          <h3 className="demo-section-title">
            3. Plan card — pending (interactive)
          </h3>
          <p className="demo-section-blurb">
            Click <strong>Approve</strong> or <strong>Refine</strong>{" "}
            below. Approve flips this card to the green "approved"
            state and clears the badge.
          </p>
          <ChatItemView
            item={liveCard}
            onApprovePlan={onApprove}
            onRejectPlan={onReject}
          />
        </section>

        <section className="demo-section">
          <h3 className="demo-section-title">4. Plan card — approved</h3>
          <p className="demo-section-blurb">
            Terminal state after Approve. Green tint, footer{" "}
            "Plan approved — plan mode exited", buttons replaced
            with the status line.
          </p>
          <ChatItemView item={approvedCard} />
        </section>

        <section className="demo-section">
          <h3 className="demo-section-title">5. Plan card — dismissed</h3>
          <p className="demo-section-blurb">
            Terminal state after Refine. Dimmed, footer{" "}
            "Plan dismissed." The plan body stays visible because
            it's part of the conversation history — the user
            just doesn't get to click those buttons again.
          </p>
          <ChatItemView item={dismissedCard} />
        </section>

        <section className="demo-section">
          <h3 className="demo-section-title">
            6. Live checklist — mid-execution (approved + agent working)
          </h3>
          <p className="demo-section-blurb">
            After Approve, as the agent calls{" "}
            <code>todo_write</code> to update each step's status,
            the checklist re-renders in place. First task done
            (struck through, green check), second in flight
            (pulsing orange dot, label switches to the gerund
            form — "Adding…" not "Add…"), rest still pending.
          </p>
          <ChatItemView item={inFlightCard} />
        </section>

        <section className="demo-section">
          <h3 className="demo-section-title">7. Live checklist — done</h3>
          <p className="demo-section-blurb">
            Terminal state once the agent has called{" "}
            <code>todo_write</code> with every task marked
            <code>completed</code>. Each row struck through with
            a green checkmark — visible proof that the plan the
            user approved earlier actually got executed.
          </p>
          <ChatItemView item={doneCard} />
        </section>

        <section className="demo-section">
          <h3 className="demo-section-title">Full flow</h3>
          <ol className="demo-section-blurb">
            <li>
              User asks for a complex task (e.g. multi-file refactor).
            </li>
            <li>
              Agent calls <code>enter_plan_mode(reason)</code> —
              badge animates on, info banner injects.
            </li>
            <li>
              Agent reads files, searches, gathers context (writes
              blocked by <code>PermissionEvaluator.PLAN</code>).
            </li>
            <li>
              Agent calls <code>exit_plan_mode(plan)</code> — plan
              card appears with Approve / Refine.
            </li>
            <li>
              User clicks <strong>Approve</strong> →{" "}
              <code>/plan off</code> runs automatically → badge
              disappears, card turns green, agent's next turn
              executes.
            </li>
          </ol>
        </section>
      </main>
    </div>
  );
}
