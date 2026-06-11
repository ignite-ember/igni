import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applyEvent,
  assistantItem,
  errorItem,
  infoItem,
  newStreamState,
  shellItem,
  userItem,
  type ChatItem,
} from "./chat/model";
import { ChatItemView } from "./components/ChatItems";
import { Composer, BUILTIN_COMMANDS, type SlashCommand } from "./components/Composer";
import { HitlDialog, type HitlDecision } from "./components/HitlDialog";
import { Sidebar, type SessionEntry } from "./components/Sidebar";
import { CodeIndexPanel } from "./components/panels/CodeIndexPanel";
import { DetailsPanel } from "./components/panels/DetailsPanel";
import { InfoPanel } from "./components/panels/InfoPanel";
import { LoginPanel } from "./components/panels/LoginPanel";
import { LoopPanel } from "./components/panels/LoopPanel";
import { McpPanel } from "./components/panels/McpPanel";
import { SchedulePanel } from "./components/panels/SchedulePanel";
import { EmberClient, type ConnectionState } from "./protocol/client";
import type { HITLRequest, ServerMessage, StatusUpdate } from "./protocol/messages";

type PanelState =
  | { kind: "none" }
  | { kind: "mcp" }
  | { kind: "codeindex" }
  | { kind: "loop" }
  | { kind: "schedule" }
  | { kind: "login" }
  | { kind: "info"; title: string; markdown: string }
  | { kind: "details"; title: string; method: string; fallback: string };

interface UpdateInfo {
  available: boolean;
  current_version: string;
  latest_version: string;
  download_url?: string;
}

/** Entries for the header tools menu — each opens its feature's UI
 * directly (no slash command visible to the user). */
const TOOLS_MENU: { label: string; command: string; desc: string }[] = [
  { label: "MCP servers", command: "/mcp", desc: "connect external tools" },
  { label: "CodeIndex", command: "/codeindex", desc: "semantic code search" },
  { label: "Agents", command: "/agents", desc: "agent pool" },
  { label: "Skills", command: "/skills", desc: "slash-command workflows" },
  { label: "Plugins", command: "/plugins", desc: "marketplaces & installs" },
  { label: "Knowledge", command: "/knowledge", desc: "project knowledge base" },
  { label: "Hooks", command: "/hooks", desc: "pre/post tool hooks" },
  { label: "Loop", command: "/loop", desc: "recurring prompt status" },
  { label: "Scheduled tasks", command: "/schedule", desc: "background routines" },
  { label: "Compact context", command: "/compact", desc: "summarize old turns" },
  { label: "Help", command: "/help", desc: "all commands" },
];

interface ModelRegistry {
  default: string;
  registry: Record<string, Record<string, unknown>>;
}

export default function App() {
  const client = useMemo(() => new EmberClient(), []);
  const [conn, setConn] = useState<ConnectionState>("connecting");
  const [items, setItems] = useState<ChatItem[]>([]);
  const [processing, setProcessing] = useState(false);
  const [status, setStatus] = useState<StatusUpdate | null>(null);
  const [hitl, setHitl] = useState<HITLRequest[] | null>(null);
  const [panel, setPanel] = useState<PanelState>({ kind: "none" });
  const [sidebarOpen, setSidebarOpen] = useState(window.innerWidth > 700);
  const [sessions, setSessions] = useState<SessionEntry[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [skills, setSkills] = useState<SlashCommand[]>([]);
  const [modelMenu, setModelMenu] = useState<
    { name: string; current: boolean }[] | null
  >(null);
  const [accountMenu, setAccountMenu] = useState(false);
  const [update, setUpdate] = useState<UpdateInfo | null>(null);
  // Live draft from another attached view (mirroring).
  const [remoteDraft, setRemoteDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const processingRef = useRef(false);
  // <think>-tag parser state. `usesThinkTags` persists across runs
  // (the model identity doesn't change mid-session unless switched);
  // inThinking/carry reset at each run start.
  const streamRef = useRef(newStreamState());

  const append = useCallback(
    (item: ChatItem) => setItems((prev) => [...prev, item]),
    [],
  );

  const setProc = useCallback((v: boolean) => {
    processingRef.current = v;
    setProcessing(v);
  }, []);

  // ── Streamed event handler (run + HITL-resume streams) ───────────
  const onStreamEvent = useCallback(
    (m: ServerMessage) => {
      if (m.type === "streaming_done") {
        // Same contract as the TUI: unblock input when content ends,
        // even though the BE tail (memory, compression) still drains.
        setProc(false);
        return;
      }
      if (m.type === "run_paused") {
        setHitl(m.requirements);
        return;
      }
      if (m.type === "status_update") {
        setStatus(m);
        return;
      }
      if (m.type === "command_result") {
        const text = m.display_content || m.content;
        if (text) {
          setItems((prev) => [
            ...prev,
            m.kind === "error" ? errorItem(text) : assistantItem(text),
          ]);
        }
        return;
      }
      setItems((prev) => applyEvent(prev, m, streamRef.current));
    },
    [setProc],
  );

  const refreshStatus = useCallback(async () => {
    try {
      const s = await client.rpc<StatusUpdate>("get_status");
      if (s) setStatus(s);
    } catch {
      /* disconnected */
    }
  }, [client]);

  const refreshSessions = useCallback(async () => {
    try {
      const list = await client.rpc<Record<string, unknown>[]>("list_sessions");
      setSessions(
        (list || []).map((s) => ({
          session_id: String(s.session_id ?? s.id ?? ""),
          name: String(s.name ?? s.session_id ?? "?"),
          detail: String(s.created_at ?? ""),
        })),
      );
    } catch {
      /* older BE or empty */
    }
  }, [client]);

  // ── Wiring ────────────────────────────────────────────────────────
  useEffect(() => {
    const offState = client.onStateChange((s) => {
      setConn(s);
      if (s === "connected") {
        void (async () => {
          try {
            // Adopt the BE default session as this view's binding
            // (unless the tab already bound one before a reconnect).
            if (!client.sessionId) {
              client.sessionId = await client.rpc<string>("get_session_id");
            }
            setSessionId(client.sessionId);
          } catch {
            /* ignore */
          }
          void refreshStatus();
          void refreshSessions();
          try {
            const defs = await client.rpc<{ name: string; description: string }[]>(
              "get_skill_definitions",
            );
            setSkills(
              (defs || []).map((d) => ({
                name: `/${d.name}`,
                description: d.description || "skill",
              })),
            );
          } catch {
            /* skills optional */
          }
          try {
            const info = await client.rpc<UpdateInfo | null>("check_for_update");
            if (info?.available) setUpdate(info);
          } catch {
            /* update check is best-effort */
          }
        })();
      }
    });
    const offEvent = client.onEvent((m) => {
      // ── Mirroring events ────────────────────────────────────────
      if (m.type === "welcome") return; // client stored its id
      if (m.type === "typing") {
        if (m.client_id !== client.clientId) setRemoteDraft(m.text);
        return;
      }
      if (m.type === "user_message_received") {
        // Another view submitted — paint its bubble here. Our own
        // echo is skipped (we already painted on submit).
        if (m.client_id !== client.clientId) {
          setRemoteDraft("");
          append(userItem(m.text));
          if (m.queued) append(infoItem("Queued — will run after the current turn."));
        }
        return;
      }
      if (m.type === "requirement_resolved") {
        // Another view answered the permission dialog — drop it here.
        setHitl((prev) => {
          if (!prev) return prev;
          const left = prev.filter((r) => r.requirement_id !== m.requirement_id);
          return left.length ? left : null;
        });
        return;
      }

      if (m.type === "run_paused") setHitl(m.requirements);
      else if (m.type === "status_update") setStatus(m);
      else if (m.type === "push_notification") {
        if (m.channel === "background_process_done") {
          const p = m.payload as { cmd?: string; exit_code?: number };
          append(infoItem(`Background process finished (exit ${p.exit_code}): ${p.cmd}`));
        } else if (m.channel === "scheduler_started") {
          append(infoItem(`Scheduled task started: ${m.payload.description ?? ""}`));
        } else if (m.channel === "scheduler_completed") {
          append(infoItem(`Scheduled task completed: ${m.payload.description ?? ""}`));
        } else if (m.channel === "orchestrate_progress") {
          // Rendered inside tool cards by the TUI; keep as dim info.
          append({ kind: "agent", id: Date.now(), text: String(m.payload.line ?? "") });
        }
      } else if (m.type === "streaming_done" || m.type === "stream_end") {
        // A run initiated by ANOTHER view finished its content.
        setProc(false);
      } else if (m.type === "run_started") {
        // A run initiated by another view began — reflect busy state
        // so submits from this view queue instead of racing.
        setProc(true);
        setItems((prev) => applyEvent(prev, m, streamRef.current));
      } else {
        // Remaining streamed events (content deltas, tool cards...)
        // from runs other views started: render them identically.
        setItems((prev) => applyEvent(prev, m, streamRef.current));
      }
    });
    client.connect();
    return () => {
      offState();
      offEvent();
      client.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  // Status poll — mirrors the TUI status-bar cadence.
  useEffect(() => {
    if (conn !== "connected") return;
    const t = setInterval(refreshStatus, 5_000);
    return () => clearInterval(t);
  }, [conn, refreshStatus]);

  // Esc cancels the in-flight run (TUI parity).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && processingRef.current && !hitl) {
        client.cancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [client, hitl]);

  // Autoscroll on new content.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items, processing]);

  // ── Loop continuation (TUI parity: refire after each run) ────────
  const continueLoopIfPending = useCallback(async () => {
    try {
      const next = await client.rpc<{ prompt?: string } | null>(
        "pop_pending_loop_iteration",
      );
      if (next?.prompt) {
        append(infoItem(`↻ loop: ${next.prompt}`));
        await runUserMessage(next.prompt);
      }
    } catch {
      /* no loop pending */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  const runUserMessage = useCallback(
    async (text: string) => {
      setProc(true);
      // Fresh turn: close any dangling thinking state from a
      // cancelled run; keep usesThinkTags (model didn't change).
      streamRef.current.inThinking = false;
      streamRef.current.carry = "";
      try {
        await client.runMessage(text, onStreamEvent);
      } catch (e) {
        append(errorItem(String(e)));
      } finally {
        setProc(false);
        void continueLoopIfPending();
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, onStreamEvent],
  );

  // ── Slash command routing ─────────────────────────────────────────
  // `echo=false` is the UI-affordance path (menu/button clicks): the
  // command runs silently instead of appearing as a fake typed
  // message in the chat. Typed slash commands keep echoing.
  const runCommand = useCallback(
    async (text: string, echo = true) => {
      if (echo) append(userItem(text));
      try {
        const result = await client.handleCommand(text);
        if (result.type !== "command_result") {
          onStreamEvent(result);
          return;
        }
        const content = result.display_content || result.content;
        switch (result.action) {
          case "clear": {
            setItems([]);
            try {
              // /clear renews the runtime's session id — rebind so
              // this view follows the fresh conversation.
              const renewed = await client.rpc<string>("get_session_id");
              client.sessionId = renewed;
              setSessionId(renewed);
            } catch {
              /* ignore */
            }
            append(infoItem("New conversation started."));
            void refreshSessions();
            return;
          }
          case "sessions":
            setSidebarOpen(true);
            void refreshSessions();
            return;
          case "model": {
            const reg = await client.rpc<ModelRegistry>("get_model_registry");
            setModelMenu(
              Object.keys(reg.registry)
                .sort()
                .map((name) => ({ name, current: name === reg.default })),
            );
            return;
          }
          case "model_switched":
            if (content) append(infoItem(content));
            void refreshStatus();
            return;
          case "login":
            setPanel({ kind: "login" });
            return;
          case "mcp":
            setPanel({ kind: "mcp" });
            return;
          case "codeindex":
            setPanel({ kind: "codeindex" });
            return;
          case "loop":
            if (content && echo) append(assistantItem(content));
            setPanel({ kind: "loop" });
            return;
          case "schedule":
            if (content && echo) append(assistantItem(content));
            setPanel({ kind: "schedule" });
            return;
          case "agents":
          case "skills":
          case "plugins":
          case "knowledge":
          case "hooks": {
            // These actions carry no content — the TUI builds panels
            // from the details RPCs; we render the same data.
            const method = {
              agents: "get_agent_details",
              skills: "get_skill_details",
              plugins: "get_plugin_details",
              knowledge: "get_knowledge_status",
              hooks: "get_hooks_details",
            }[result.action];
            setPanel({
              kind: "details",
              title: result.action[0].toUpperCase() + result.action.slice(1),
              method,
              fallback: content,
            });
            return;
          }
          case "help": {
            const lines = [...BUILTIN_COMMANDS, ...skills].map(
              (c) => `- \`${c.name}\` — ${c.description}`,
            );
            setPanel({
              kind: "info",
              title: "Help",
              markdown: `### Commands\n\n${lines.join("\n")}`,
            });
            return;
          }
          case "run_prompt":
            // Skills expand to a prompt that runs as a user message.
            if (content) await runUserMessage(content);
            return;
          case "compact":
            append(infoItem(content || "Context compacted."));
            return;
          case "quit":
            append(infoItem("Use the window/tab close button to quit."));
            return;
          default:
            if (content) {
              onStreamEvent(result);
            }
        }
      } catch (e) {
        append(errorItem(String(e)));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, onStreamEvent, skills],
  );

  // ── Submit (message / command / shell) ────────────────────────────
  const submit = useCallback(
    async (text: string) => {
      if (text.startsWith("/")) {
        await runCommand(text);
        return;
      }
      if (text.startsWith("$")) {
        const command = text.slice(1).trim();
        const item = shellItem(command);
        append(item);
        try {
          const res = await client.runShell(command);
          setItems((prev) =>
            prev.map((it) =>
              it.id === item.id && it.kind === "shell"
                ? { ...it, output: res.output, exitCode: res.exit_code }
                : it,
            ),
          );
        } catch (e) {
          append(errorItem(String(e)));
        }
        return;
      }
      append(userItem(text));
      if (processingRef.current) {
        client.queueMessage(text);
        append(infoItem("Queued — will run after the current turn."));
        return;
      }
      await runUserMessage(text);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, runCommand, runUserMessage],
  );

  const resolveHitl = useCallback(
    async (decisions: HitlDecision[]) => {
      setHitl(null);
      setProc(true);
      try {
        await client.resolveHitlBatch(decisions, onStreamEvent);
      } catch (e) {
        append(errorItem(String(e)));
      } finally {
        setProc(false);
        void continueLoopIfPending();
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, onStreamEvent],
  );

  const pickSession = useCallback(
    async (id: string) => {
      try {
        // Bind this VIEW to the session — no switch_session RPC.
        // The BE's session pool lazily resumes it in its own
        // runtime, so other tabs keep their sessions running in
        // parallel. First contact can take a few seconds (Session
        // construction), hence the loading note.
        client.sessionId = id;
        setSessionId(id);
        setItems([]);
        append(infoItem(`Loading session ${id}…`));
        // Load persisted history for the resumed session (TUI parity).
        try {
          const history = await client.rpc<Record<string, unknown>[]>(
            "get_chat_history",
            { session_id: id },
          );
          const loaded: ChatItem[] = [];
          for (const turn of history || []) {
            const role = String(turn.role ?? "");
            const content = String(turn.content ?? "");
            if (!content) continue;
            if (role === "user") loaded.push(userItem(content));
            else if (role === "assistant") loaded.push(assistantItem(content));
          }
          setItems(loaded);
        } catch {
          /* no history RPC result — start empty */
        }
        // Crash recovery (TUI parity): surface messages whose run
        // never completed so the user sees their interrupted prompt.
        try {
          const pending = await client.rpc<Record<string, unknown>[]>(
            "get_pending_messages",
            { session_id: id },
          );
          if (pending?.length) {
            for (const p of pending) {
              const text = String(p.text ?? "");
              if (text) append(userItem(text));
            }
            append(
              infoItem(
                `${pending.length} message(s) above were interrupted before completion — the agent knows and can pick up where it left off.`,
              ),
            );
          }
        } catch {
          /* pending-message recovery is best-effort */
        }
        append(infoItem(`Resumed session ${id}.`));
      } catch (e) {
        append(errorItem(String(e)));
      }
      if (window.innerWidth <= 700) setSidebarOpen(false);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client],
  );

  const pickModel = useCallback(
    async (name: string) => {
      setModelMenu(null);
      try {
        const res = await client.switchModel(name);
        if (res.type === "info") append(infoItem(res.text));
        void refreshStatus();
      } catch (e) {
        append(errorItem(String(e)));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client],
  );

  // ── Render ────────────────────────────────────────────────────────
  const ctxPct = status
    ? Math.round((status.context_tokens / Math.max(status.max_context, 1)) * 100)
    : 0;

  return (
    <div className="shell">
      <Sidebar
        open={sidebarOpen}
        sessions={sessions}
        currentId={sessionId}
        onNewChat={() => void runCommand("/clear", false)}
        onPick={(id) => void pickSession(id)}
        onClose={() => setSidebarOpen(false)}
      />

      <div className="main">
        {update && (
          <div className="update-banner">
            Update available: {update.current_version} → {update.latest_version}
            {update.download_url && (
              <a href={update.download_url} target="_blank" rel="noreferrer">
                get it
              </a>
            )}
            <button className="icon-btn" onClick={() => setUpdate(null)}>
              ✕
            </button>
          </div>
        )}
        <header className="app-header">
          <button
            className="icon-btn"
            title="Toggle sessions"
            onClick={() => setSidebarOpen(!sidebarOpen)}
          >
            ☰
          </button>
          {!sidebarOpen && (
            <div className="brand">
              <div className="brand-flame" />
              <span>Ember Code</span>
            </div>
          )}
          <div className="header-spacer" />
          <button
            className="chip"
            title="Switch model"
            onClick={() => void runCommand("/model", false)}
          >
            <span className="chip-label">{status?.model || "model"}</span> ▾
          </button>
          {status?.cloud_connected ? (
            <button
              className="chip"
              title="Account"
              onClick={() => setAccountMenu(!accountMenu)}
            >
              <span className="chip-label">☁ {status.cloud_org}</span> ▾
            </button>
          ) : (
            <button
              className="chip"
              style={{
                background: "var(--ember-gradient)",
                color: "#fff",
                border: "none",
              }}
              onClick={() => setPanel({ kind: "login" })}
            >
              Log in
            </button>
          )}
          {conn === "replaced" ? (
            <button
              className="chip"
              title="This chat was opened in another tab/window. Click to take over here."
              onClick={() => client.connect()}
            >
              <span className="dot disconnected" />
              <span className="chip-label">opened elsewhere — reconnect</span>
            </button>
          ) : (
            <span className="chip" style={{ cursor: "default" }} title={`backend ${conn}`}>
              <span className={`dot ${conn}`} />
              <span className="chip-label">{conn}</span>
            </span>
          )}

          {modelMenu && (
            <>
              <div
                style={{ position: "fixed", inset: 0, zIndex: 39 }}
                onClick={() => setModelMenu(null)}
              />
              <div className="dropdown">
                {modelMenu.map((m) => (
                  <div
                    key={m.name}
                    className={`popup-item ${m.current ? "active" : ""}`}
                    onClick={() => void pickModel(m.name)}
                  >
                    <span className="cmd">{m.name}</span>
                    {m.current && <span className="desc">current</span>}
                  </div>
                ))}
              </div>
            </>
          )}
          {accountMenu && (
            <>
              <div
                style={{ position: "fixed", inset: 0, zIndex: 39 }}
                onClick={() => setAccountMenu(false)}
              />
              <div className="dropdown" style={{ width: 220 }}>
                <div className="popup-item" style={{ cursor: "default" }}>
                  <span className="desc">Signed in to {status?.cloud_org}</span>
                </div>
                <div
                  className="popup-item"
                  onClick={() => {
                    setAccountMenu(false);
                    void runCommand("/logout", false);
                  }}
                >
                  <span className="cmd">Log out</span>
                </div>
              </div>
            </>
          )}
        </header>

        <div className="conversation" ref={scrollRef}>
          <div className={`col${processing ? " streaming" : ""}`}>
            {items.length === 0 && (
              <div className="welcome">
                <div
                  className="brand-flame"
                  style={{ width: 52, height: 52, margin: "0 auto", borderRadius: 14 }}
                />
                <h1>Ember Code</h1>
                <p>Your AI coding agent, in this project.</p>
                <div className="welcome-hints">
                  <button className="chip" onClick={() => void runCommand("/help", false)}>
                    Commands
                  </button>
                  <button className="chip" onClick={() => void runCommand("/model", false)}>
                    Pick a model
                  </button>
                  <button
                    className="chip"
                    onClick={() => void runCommand("/codeindex", false)}
                  >
                    CodeIndex
                  </button>
                  <button className="chip" onClick={() => void runCommand("/agents", false)}>
                    Agents
                  </button>
                </div>
              </div>
            )}
            {items.map((item) => (
              <ChatItemView key={item.id} item={item} />
            ))}
            {processing && (
              <div className="msg-info" style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <span className="dot connecting" />
                Thinking… <span style={{ color: "var(--fg-faint)" }}>Esc to cancel</span>
              </div>
            )}
          </div>
        </div>

        {remoteDraft && (
          <div className="remote-draft">
            <span className="remote-draft-pen">✎</span> another window:&nbsp;
            <span className="remote-draft-text">{remoteDraft}</span>
            <span className="remote-caret">▍</span>
          </div>
        )}
        <Composer
          client={client}
          connected={conn === "connected"}
          processing={processing}
          skills={skills}
          tools={TOOLS_MENU}
          onTool={(cmd) => void runCommand(cmd, false)}
          onTyping={(t) => client.sendTyping(t)}
          onSubmit={(t) => void submit(t)}
          onStop={() => client.cancel()}
        />
        <div className="statusline" style={{ marginTop: 0, paddingBottom: 8 }}>
          {status && (
            <>
              <span>{status.model}</span>
              <span>session {sessionId || "—"}</span>
              {status.cloud_connected && <span>☁ {status.cloud_org}</span>}
              <span>ctx {ctxPct}%</span>
            </>
          )}
        </div>
      </div>

      {hitl && <HitlDialog requirements={hitl} onResolve={(d) => void resolveHitl(d)} />}
      {panel.kind === "mcp" && (
        <McpPanel client={client} onClose={() => setPanel({ kind: "none" })} />
      )}
      {panel.kind === "codeindex" && (
        <CodeIndexPanel client={client} onClose={() => setPanel({ kind: "none" })} />
      )}
      {panel.kind === "loop" && (
        <LoopPanel client={client} onClose={() => setPanel({ kind: "none" })} />
      )}
      {panel.kind === "schedule" && (
        <SchedulePanel client={client} onClose={() => setPanel({ kind: "none" })} />
      )}
      {panel.kind === "info" && (
        <InfoPanel
          title={panel.title}
          markdown={panel.markdown}
          onClose={() => setPanel({ kind: "none" })}
        />
      )}
      {panel.kind === "details" && (
        <DetailsPanel
          client={client}
          title={panel.title}
          method={panel.method}
          fallbackMarkdown={panel.fallback}
          onClose={() => setPanel({ kind: "none" })}
        />
      )}
      {panel.kind === "login" && (
        <LoginPanel
          client={client}
          onDone={(ok, detail) => {
            setPanel({ kind: "none" });
            append(ok ? infoItem(`Logged in as ${detail}`) : errorItem(`Login failed: ${detail}`));
            void refreshStatus();
          }}
        />
      )}
    </div>
  );
}
