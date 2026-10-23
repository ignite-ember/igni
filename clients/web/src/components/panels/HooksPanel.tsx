import { useCallback, useEffect, useMemo, useState } from "react";
import type { IgniClient } from "../../protocol/client";
import { Drawer } from "./Drawer";
import { Skeleton } from "@igni/ui-react";
import { host } from "../../lib/host";

// ── Types ────────────────────────────────────────────────────────

interface HookEntry {
  event: string;
  type: "command" | "webhook" | string;
  command: string;
  url: string;
  matcher: string;
  timeout_ms: number;
  background: boolean;
  headers: Record<string, string>;
}

// Canonical event ordering. Anything we don't recognize falls in at
// the end (alphabetical) — the BE may grow new event kinds before
// the FE knows about them.
const EVENT_ORDER = [
  "SessionStart",
  "UserPromptSubmit",
  "PreToolUse",
  "PostToolUse",
  "PostToolUseFailure",
  "Notification",
  "Stop",
  "SubagentStop",
];

const EVENT_BLURB: Record<string, string> = {
  SessionStart: "Runs when a session boots — good for seeding context.",
  UserPromptSubmit: "Fires before each user message is processed.",
  PreToolUse: "Runs before a tool call. Can block or rewrite the call.",
  PostToolUse: "Runs after a successful tool call.",
  PostToolUseFailure: "Runs when a tool call fails.",
  Notification: "Side-channel notifications (HITL prompts, etc).",
  Stop: "Runs when the model finishes responding.",
  SubagentStop: "Runs when a sub-agent finishes.",
};

// ── Panel ────────────────────────────────────────────────────────

export function HooksPanel({
  client,
  onClose,
}: {
  client: IgniClient;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<HookEntry[] | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setError("");
    try {
      const r = (await client.rpc<HookEntry[]>("get_hooks_details")) || [];
      setRows(Array.isArray(r) ? r : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRows([]);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Dedupe (event + type + command + url + matcher) — the BE
  // sometimes double-counts hooks that exist in both project-local
  // and plugin sources. The FE just shows them once.
  const deduped = useMemo(() => {
    if (!rows) return null;
    const seen = new Set<string>();
    const out: HookEntry[] = [];
    for (const h of rows) {
      const key = [h.event, h.type, h.command, h.url, h.matcher].join("\u0000");
      // A literal NUL byte lived in this string rather than the
      // escape. It is the right separator — no field can contain
      // one, so no two distinct hooks can collide on the joined key
      // — but written raw it made the whole file read as binary:
      // `file` reported "data", and `grep -rl` skipped it, which is
      // how this component survived a rename that touched every
      // other file importing the same symbol.
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(h);
    }
    return out;
  }, [rows]);

  // Group by event, in canonical order then alphabetical for unknowns.
  const groups = useMemo(() => {
    if (!deduped) return null;
    const byEvent = new Map<string, HookEntry[]>();
    for (const h of deduped) {
      const list = byEvent.get(h.event) || [];
      list.push(h);
      byEvent.set(h.event, list);
    }
    const ordered: { event: string; hooks: HookEntry[] }[] = [];
    for (const ev of EVENT_ORDER) {
      const hs = byEvent.get(ev);
      if (hs) {
        ordered.push({ event: ev, hooks: hs });
        byEvent.delete(ev);
      }
    }
    for (const ev of [...byEvent.keys()].sort()) {
      ordered.push({ event: ev, hooks: byEvent.get(ev)! });
    }
    return ordered;
  }, [deduped]);

  const total = deduped?.length ?? 0;

  return (
    <Drawer
      title="Hooks"
      onClose={onClose}
      headerExtras={
        deduped !== null ? (
          <span className="hooks-count">
            {total} {total === 1 ? "hook" : "hooks"}
          </span>
        ) : null
      }
    >
      {error && <div className="msg-error" style={{ marginBottom: 10 }}>{error}</div>}
      {groups === null ? (
        <HooksSkeleton />
      ) : groups.length === 0 ? (
        <EmptyHero />
      ) : (
        <div className="hooks-groups">
          {groups.map(({ event, hooks }) => (
            <HookGroup key={event} event={event} hooks={hooks} />
          ))}
        </div>
      )}
    </Drawer>
  );
}

// ── Group ────────────────────────────────────────────────────────

function HookGroup({ event, hooks }: { event: string; hooks: HookEntry[] }) {
  const blurb = EVENT_BLURB[event];
  return (
    <section className="hooks-group">
      <header className="hooks-group-head">
        <span className="hooks-event-name">{event}</span>
        <span className="hooks-event-count">{hooks.length}</span>
      </header>
      {blurb && <div className="hooks-event-blurb">{blurb}</div>}
      <div className="hooks-list">
        {hooks.map((h, i) => (
          <HookRow key={i} hook={h} />
        ))}
      </div>
    </section>
  );
}

// ── Row ──────────────────────────────────────────────────────────

function HookRow({ hook }: { hook: HookEntry }) {
  const isWebhook = hook.type === "webhook" || !!hook.url;
  const ident = isWebhook ? hook.url || "(no url)" : hook.command || "(no command)";
  const tags: { label: string; tone?: string }[] = [];
  if (hook.matcher) tags.push({ label: `matches: ${hook.matcher}` });
  if (hook.background) tags.push({ label: "background", tone: "tone-good" });
  if (hook.timeout_ms && hook.timeout_ms !== 10000) {
    tags.push({ label: `${(hook.timeout_ms / 1000).toFixed(0)}s timeout` });
  }

  const Inner = (
    <>
      <div className="hooks-row-icon" aria-hidden="true">
        {isWebhook ? <WebhookIcon /> : <ScriptIcon />}
      </div>
      <div className="hooks-row-main">
        <code className="hooks-row-ident" title={ident}>{ident}</code>
        {tags.length > 0 && (
          <div className="hooks-row-tags">
            {tags.map((t, i) => (
              <span key={i} className={`hooks-tag ${t.tone || ""}`.trim()}>
                {t.label}
              </span>
            ))}
          </div>
        )}
      </div>
    </>
  );

  // Webhook URLs aren't files — open externally if at all. Script
  // commands go through the host bridge: native IDE in VSCode /
  // JetBrains / Tauri, preview modal in plain web.
  if (isWebhook) {
    return <div className="hooks-row">{Inner}</div>;
  }
  return (
    <button
      type="button"
      className="hooks-row hooks-row-link"
      title={
        host.canOpenNatively
          ? `Open ${ident} in your editor`
          : `Preview ${ident}`
      }
      onClick={() => void host.openFile(ident)}
    >
      {Inner}
    </button>
  );
}

// ── Empty + skeleton ─────────────────────────────────────────────

function EmptyHero() {
  return (
    <div className="kb-empty">
      <div className="kb-empty-title">No hooks configured</div>
      <div className="kb-empty-hint">
        Drop scripts into <code>.igni/hooks/</code> or declare hooks under{" "}
        <code>hooks</code> in <code>.igni/settings.json</code>. Each hook binds
        a lifecycle event (e.g. <code>PreToolUse</code>) to a script or webhook
        — see the docs for matcher patterns and the JSON payload shape.
      </div>
    </div>
  );
}

function HooksSkeleton() {
  return (
    <div className="hooks-groups">
      {[1, 2].map((g) => (
        <div className="hooks-group" key={g}>
          <Skeleton.Line width="40%" height={13} />
          <Skeleton.Rows count={3} style={{ marginTop: 12 }} />
        </div>
      ))}
    </div>
  );
}

// ── Inline SVGs ──────────────────────────────────────────────────

function ScriptIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path
        d="M3 2.5h7.5L13 5v8.5H3z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path d="M5 8h6M5 10.5h4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

function WebhookIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <circle cx="6.5" cy="5.5" r="1.5" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="11.5" cy="11" r="1.5" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="4" cy="12" r="1.5" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M7.6 6.8l2.6 3.4M5.4 6.6L4.6 10.4M5.6 12.6h4.4"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}
