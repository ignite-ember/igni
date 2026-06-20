import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applyEvent,
  applyOrchestrateEvent,
  assistantItem,
  compactItem,
  correctStatsCtx,
  restoredItem,
  errorItem,
  infoItem,
  isOrchestrateActive,
  loopItem,
  newStreamState,
  shellItem,
  userItem,
  type ChatItem,
  type OrchestrateEvent,
} from "./chat/model";
import { ClientStateStore, ensureClientId } from "./clientState";
import { ChatItemView } from "./components/ChatItems";
import { Composer, BUILTIN_COMMANDS, type SlashCommand } from "./components/Composer";
import { CodeIndexIndicator } from "./components/CodeIndexIndicator";
import { CtxMeter, SessionChip } from "./components/StatusBits";
import { HitlDialog, type HitlDecision } from "./components/HitlDialog";
import {
  ChevronIcon,
  CloudIcon,
  FlameIcon,
  FolderIcon,
  MenuIcon,
} from "./components/Icons";
import { ScrollIndicator } from "./components/ScrollIndicator";
import { Sidebar, type SessionEntry } from "./components/Sidebar";
import { AgentsPanel } from "./components/panels/AgentsPanel";
import { CodeIndexPanel } from "./components/panels/CodeIndexPanel";
import { DetailsPanel } from "./components/panels/DetailsPanel";
import { FilePreview } from "./components/FilePreview";
import { HooksPanel } from "./components/panels/HooksPanel";
import { KnowledgePanel } from "./components/panels/KnowledgePanel";
import { Toasts, type Toast } from "./components/Toasts";
import { UpdatePrompt } from "./components/UpdatePrompt";
import { host } from "./lib/host";
import { PluginsPanel } from "./components/panels/PluginsPanel";
import { SkillsPanel } from "./components/panels/SkillsPanel";
import { DirectoryPicker } from "./components/panels/DirectoryPicker";
import { InfoPanel } from "./components/panels/InfoPanel";
import { LoginPanel } from "./components/panels/LoginPanel";
import { LoopPanel } from "./components/panels/LoopPanel";
import { McpPanel } from "./components/panels/McpPanel";
import { SchedulePanel } from "./components/panels/SchedulePanel";
import { EmberClient, pickNativeDirectory, type ConnectionState } from "./protocol/client";
import type { HITLRequest, ServerMessage, StatusUpdate } from "./protocol/messages";

type PanelState =
  | { kind: "none" }
  | { kind: "mcp" }
  | { kind: "codeindex" }
  | { kind: "loop" }
  | { kind: "schedule" }
  | { kind: "login" }
  | { kind: "info"; title: string; markdown: string }
  | { kind: "details"; title: string; method: string; fallback: string }
  | { kind: "agents" }
  | { kind: "skills" }
  | { kind: "plugins" }
  | { kind: "knowledge" }
  | { kind: "hooks" }
  | { kind: "dir-picker" };

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
  { label: "Context breakdown", command: "/ctx", desc: "system vs runs token split" },
  { label: "Help", command: "/help", desc: "all commands" },
];

export default function App() {
  const client = useMemo(() => new EmberClient(), []);
  const [conn, setConn] = useState<ConnectionState>("connecting");
  const [items, setItems] = useState<ChatItem[]>([]);
  const [processing, setProcessing] = useState(false);
  // True between ``streaming_done`` and ``run_completed`` — the BE
  // tail (memory writeback, stats roll-up) is still draining. Input
  // is already unblocked (``processing`` is false), but we keep the
  // "Ember is replying…" indicator visible so the UI doesn't claim
  // "done" while the tokens line is still settling.
  const [finalizing, setFinalizing] = useState(false);
  const [status, setStatus] = useState<StatusUpdate | null>(null);
  const [hitl, setHitl] = useState<HITLRequest[] | null>(null);
  const [panel, setPanel] = useState<PanelState>({ kind: "none" });
  const [composerSeed, setComposerSeed] = useState<{ text: string; n: number } | null>(null);
  // Plain-browser fallback for host.openFile — bridge-equipped hosts
  // (Tauri / VSCode / JetBrains) handle the open themselves and never
  // set this. Tracked at the App level so any panel can call
  // host.openFile and get a consistent result.
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  useEffect(() => {
    host.setPreviewFallback((p) => setPreviewPath(p));
  }, []);

  // Host-aware CSS hook. The Tauri shell sets these via its
  // ``initialization_script`` at page-load time, but Tauri 2 doesn't
  // re-fire that script when the loading view navigates to the real
  // app via ``location.href``. Re-stamp from the React mount so the
  // host-aware rules in theme.css (custom title bar, traffic-light
  // gutter, drag region) always match. Idempotent — re-running just
  // sets the same attributes.
  useEffect(() => {
    const html = typeof document !== "undefined" ? document.documentElement : null;
    if (!html) return;
    const w = window as unknown as { __TAURI__?: unknown };
    if (!w.__TAURI__) return;
    html.dataset.host = "tauri";
    const p = (navigator.platform || "").toLowerCase();
    html.dataset.platform = /mac/.test(p)
      ? "mac"
      : /win/.test(p)
        ? "win"
        : "linux";
  }, []);

  // IDE → web-UI event bus. Two paths converge here:
  //
  //   • JetBrains dispatches ``ember-host`` CustomEvents on
  //     ``window`` via ``executeJavaScript``.
  //   • VSCode dispatches plain ``message`` events via
  //     ``webview.postMessage`` — the standard webview channel.
  //
  // Both sources carry the same shape (``{type, payload}`` for the
  // CustomEvent detail, or ``{type, ...fields}`` for postMessage).
  // We normalise into ``(type, payload)`` and feed a single dispatch
  // so the rest of the handler is host-agnostic. Sub-handlers seed
  // the composer with a structured form the model will interpret
  // correctly — ``@<path>:start-end`` reference + fenced block.
  useEffect(() => {
    const dispatch = (type: string, payload: Record<string, unknown>) => {
      if (type === "ember:addToComposer") {
        const text = String(payload.text ?? "");
        const path = payload.path ? String(payload.path) : null;
        const line = typeof payload.line === "number" ? payload.line : null;
        const endLine = typeof payload.end_line === "number" ? payload.end_line : null;
        const range =
          line != null
            ? `:${line}${endLine != null && endLine !== line ? `-${endLine}` : ""}`
            : "";
        const ref = path ? `@${path}${range}\n` : "";
        setComposerSeed({ text: `${ref}\`\`\`\n${text}\n\`\`\``, n: Date.now() });
      } else if (type === "ember:attachFile") {
        const path = payload.path ? String(payload.path) : null;
        if (path) setComposerSeed({ text: `@${path}`, n: Date.now() });
      } else if (type === "ember:menu") {
        // Native-menu items routed through the host bridge. Tauri
        // and (future) JetBrains menu hooks both emit these. ``id``
        // is the menu item identifier the native side registered.
        const id = String(payload.id ?? "");
        if (id === "new_chat") {
          // Empty the chat — same effect as the ``/clear`` command.
          setItems([]);
        } else if (id === "check_update") {
          // App-menu "Check for Updates…" — fire the explicit check
          // with feedback (``silent=false`` surfaces an OS "Up to
          // date" notification when there's nothing new). Routed
          // through a ref so this listener can stay mounted once
          // and still call the latest closure.
          void checkForUpdateRef.current?.(false);
        } else if (id === "restart_backend") {
          // Best-effort reconnect signal. The native side already
          // restarts the BE process; here we just kick the WS
          // client to re-connect so it doesn't sit on the closed
          // socket. ``client.connect()`` is idempotent.
          try {
            client.close();
          } catch {
            /* already closed */
          }
        }
      } else if (type === "ember:theme") {
        // IDE theme override — wins over OS ``prefers-color-scheme``.
        // The CSS already keys off ``html[data-theme]`` (see
        // ``theme.css``); we just toggle the attribute. When the
        // bridge disconnects (web build, no IDE), the attribute is
        // never set and the OS-detected default takes over.
        const dark = Boolean(payload.dark);
        document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
      }
    };
    const onCustom = (e: Event) => {
      const ev = e as CustomEvent<{ type: string; payload: Record<string, unknown> }>;
      const { type, payload } = ev.detail || { type: "", payload: {} };
      dispatch(type, payload || {});
    };
    const onMessage = (e: MessageEvent) => {
      const data = e.data as { type?: string; payload?: Record<string, unknown> } & Record<string, unknown>;
      if (!data || typeof data.type !== "string") return;
      // ``ember:searchCodeResult`` is correlation-id traffic owned
      // by ``host.searchCode`` — don't dispatch as a normal event.
      if (data.type === "ember:searchCodeResult") return;
      // Some VSCode messages use a nested ``payload``; others (the
      // older shape) inline fields. Tolerate both.
      const payload = (data.payload as Record<string, unknown>) ?? data;
      dispatch(data.type, payload);
    };
    window.addEventListener("ember-host", onCustom);
    window.addEventListener("message", onMessage);
    return () => {
      window.removeEventListener("ember-host", onCustom);
      window.removeEventListener("message", onMessage);
    };
  }, []);

  // Toast stack — non-conversational notifications (scheduled tasks
  // completing, background runs finishing) land here so they survive
  // conversation switches and don't pollute the active chat. Persists
  // top-right; auto-dismiss inside each card.
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastIdRef = useRef(1);
  const addToast = useCallback((toast: Omit<Toast, "id">) => {
    setToasts((prev) => [...prev, { id: toastIdRef.current++, ...toast }]);
  }, []);
  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);
  // Single dispatch for user-facing notifications. Tries the native
  // host bridge first (Tauri → macOS Notification Center; VSCode →
  // ``window.showInformationMessage``; JetBrains → balloon). Only
  // falls back to the in-app toast stack on plain web where no
  // bridge exists.
  const notifyHost = useCallback(
    async (payload: {
      title: string;
      body?: string;
      onClick?: () => void;
      data?: Record<string, unknown>;
    }) => {
      const delivered = await host.notify({
        title: payload.title,
        body: payload.body,
        data: payload.data,
      });
      if (delivered) return;
      addToast({
        title: payload.title,
        body: payload.body,
        onClick: payload.onClick,
      });
    },
    [addToast],
  );
  // In-app modal sheet for the update action surface. The
  // alerting surface is the host OS notification — see
  // ``announceUpdate``. ``null`` means no update pending or the
  // user picked "Later".
  const [pendingUpdate, setPendingUpdate] = useState<UpdateInfo | null>(null);
  const [showUpdateModal, setShowUpdateModal] = useState(false);
  // Ref to the latest ``checkForUpdate`` closure so the once-mounted
  // ``ember-host`` listener (deps ``[]``) can call the current version.
  const checkForUpdateRef = useRef<((silent?: boolean) => Promise<void>) | null>(
    null,
  );
  const announceUpdate = useCallback(
    (info: UpdateInfo) => {
      if (info.available) {
        setPendingUpdate(info);
        setShowUpdateModal(true);
        // OS-level alert so the user sees it even if the app is
        // backgrounded. The modal sheet takes over once they
        // refocus the window.
        void host.notify({
          title: "Update available",
          body: `Ember Code ${info.latest_version} is ready to install.`,
        });
      }
    },
    [],
  );
  // Re-runnable update check used by the native "Check for Updates…"
  // menu item and the version chip click. ``silent=false`` surfaces
  // an OS notification when there's no update, so the menu click
  // always gives the user feedback.
  const checkForUpdate = useCallback(
    async (silent = false) => {
      try {
        const tauriInvoke = (window as unknown as {
          __TAURI__?: { core?: { invoke?: (cmd: string) => Promise<unknown> } };
        }).__TAURI__?.core?.invoke;
        const info = tauriInvoke
          ? ((await tauriInvoke("ember_check_update")) as UpdateInfo)
          : await client.rpc<UpdateInfo | null>("check_for_update");
        if (!info) return;
        if (info.available) {
          announceUpdate(info);
        } else if (!silent) {
          void host.notify({
            title: "Up to date",
            body: `You're on the latest version (${info.current_version}).`,
          });
        }
      } catch {
        /* best-effort; silent failure */
      }
    },
    [announceUpdate, client],
  );
  // Keep the ref in lockstep so the menu handler always invokes the
  // freshest closure (which captures the latest ``client``).
  useEffect(() => {
    checkForUpdateRef.current = checkForUpdate;
  }, [checkForUpdate]);
  // DEV-ONLY: set ``VITE_EMBER_FAKE_UPDATE=1`` to surface the modal
  // + OS notification at launch without contacting GitHub Releases.
  // Remove before shipping.
  const fakeUpdateDone = useRef(false);
  useEffect(() => {
    if (fakeUpdateDone.current) return;
    const env = (import.meta as unknown as { env?: Record<string, string> }).env;
    if (!env?.VITE_EMBER_FAKE_UPDATE) return;
    fakeUpdateDone.current = true;
    announceUpdate({
      available: true,
      current_version: "0.6.0",
      latest_version: "0.7.0",
      download_url:
        "https://github.com/ignite-ember/ember__code/releases/latest",
    });
  }, [announceUpdate]);
  // Per-client UI state — hydrated from the BE on connect. Lives in
  // a memoized store so all three keys (session-id, sidebar, draft)
  // round-trip the same way regardless of host (browser / VSCode /
  // JetBrains). See clientState.ts.
  const clientState = useMemo(
    () => new ClientStateStore(client, ensureClientId()),
    [client],
  );
  const SESSION_KEY = "session-id";
  const SIDEBAR_KEY = "sidebar-open";
  const [sidebarOpen, setSidebarOpenState] = useState(window.innerWidth > 700);
  const setSidebarOpen = useCallback(
    (next: boolean | ((prev: boolean) => boolean)) => {
      setSidebarOpenState((prev) => {
        const resolved = typeof next === "function" ? next(prev) : next;
        clientState.set(SIDEBAR_KEY, String(resolved));
        return resolved;
      });
    },
    [clientState],
  );
  const [sessions, setSessions] = useState<SessionEntry[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [skills, setSkills] = useState<SlashCommand[]>([]);
  const [modelMenuSignal, setModelMenuSignal] = useState<{ n: number } | null>(null);
  const [accountMenu, setAccountMenu] = useState(false);
  // Live draft from another attached view (mirroring).
  // The directory this session is locked to (tools + shell cwd).
  const [projectDir, setProjectDir] = useState("");

  // Tauri title-bar bridge. macOS Finder convention: the window
  // title is the folder name; the org goes after a middle dot.
  // This effect re-runs every time projectDir or status.cloud_org
  // changes so the title stays current across login/logout and
  // project-lock switches.
  useEffect(() => {
    const setTitle = (
      window as unknown as {
        __EMBER_HOST__?: { setAppTitle?: (folder: string, org: string) => void };
      }
    ).__EMBER_HOST__?.setAppTitle;
    if (typeof setTitle !== "function") return;
    const folder = projectDir
      ? projectDir.split("/").filter(Boolean).pop() || projectDir
      : "";
    const org = status?.cloud_org || "";
    try {
      setTitle(folder, org);
    } catch {
      /* shell unavailable — title falls back to whatever the
         Tauri builder set at window create. */
    }
  }, [projectDir, status?.cloud_org]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const headerRef = useRef<HTMLElement>(null);
  const processingRef = useRef(false);

  // Bind window-drag via a native mousedown listener — React's
  // synthetic onMouseDown was firing but Tauri's drag-region path
  // wasn't, and the CSS ``-webkit-app-region: drag`` rule was being
  // skipped by WebKit when the header is absolutely positioned with
  // a near-transparent background. Native listener + a direct call
  // to ``startDragging`` bypasses every layer of hit-test indirection.
  useEffect(() => {
    const el = headerRef.current;
    if (!el) return;
    const tauri = (window as unknown as {
      __TAURI__?: {
        window?: { getCurrentWindow?: () => { startDragging: () => Promise<void> | void } };
      };
    }).__TAURI__;
    if (!tauri?.window?.getCurrentWindow) return; // not a Tauri host
    const onDown = (e: MouseEvent) => {
      if (e.button !== 0) return;
      const target = e.target as HTMLElement | null;
      if (!target) return;
      // Skip interactive children so they keep their click behaviour.
      if (target.closest("button, input, textarea, a, .chip, .file-pill, .file-pill-wrap")) {
        return;
      }
      e.preventDefault();
      try {
        const cur = tauri.window!.getCurrentWindow!();
        void cur.startDragging();
      } catch (err) {
        console.warn("[ember] startDragging failed", err);
      }
    };
    el.addEventListener("mousedown", onDown);
    return () => el.removeEventListener("mousedown", onDown);
  }, []);
  // Bumped on /clear: late events from a pre-clear run (Agno's
  // post-stream tail, e.g. run_completed) must not leak into the
  // fresh conversation.
  const viewGenRef = useRef(0);
  // <think>-tag parser state. `usesThinkTags` persists across runs
  // (the model identity doesn't change mid-session unless switched);
  // inThinking/carry reset at each run start.
  const streamRef = useRef(newStreamState());

  /** Ping the OS / favicon when a reply finishes while the tab is
   *  not focused, so the user knows to come back. */
  const notifyDone = useCallback(() => {
    // Don't bother if the user is watching the tab.
    if (typeof document !== "undefined" && document.visibilityState === "visible") return;
    try {
      if (typeof Notification === "undefined") return;
      if (Notification.permission === "granted") {
        new Notification("Ember Code", { body: "Your reply is ready.", silent: true });
      } else if (Notification.permission !== "denied") {
        // Request permission once; subsequent runs honour the choice.
        void Notification.requestPermission();
      }
    } catch {
      /* notifications unsupported / blocked */
    }
  }, []);

  const append = useCallback(
    (item: ChatItem) => setItems((prev) => [...prev, item]),
    [],
  );

  const setProc = useCallback((v: boolean) => {
    processingRef.current = v;
    setProcessing(v);
  }, []);

  const refreshStatus = useCallback(async () => {
    try {
      const s = await client.rpc<StatusUpdate>("get_status");
      if (s) setStatus(s);
    } catch {
      /* disconnected */
    }
    try {
      setProjectDir(await client.rpc<string>("get_project_dir"));
    } catch {
      /* older BE */
    }
  }, [client]);

  // ── Streamed event handler (run + HITL-resume streams) ───────────
  const onStreamEvent = useCallback(
    (m: ServerMessage) => {
      if (m.type === "streaming_done") {
        // Same contract as the TUI: unblock input when content ends,
        // even though the BE tail (memory, compression) still drains.
        // Hand off to ``finalizing`` so the indicator stays visible
        // through the tail — see the state declaration above.
        setProc(false);
        setFinalizing(true);
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
      if (m.type === "run_started") {
        // Fresh run starting on this view — wipe any leftover tail
        // flag from the previous run so the indicator lifecycle
        // resets cleanly (processing is already true via setProc).
        setFinalizing(false);
      }
      setItems((prev) => applyEvent(prev, m, streamRef.current));
      // After a top-level run finishes, Agno's reported ``input_tokens``
      // is a sum across model iterations and is 2-3× the real session
      // size on multi-step turns. The TUI long since switched its
      // context meter to ``count_context_tokens`` (Agno's per-model
      // tokenizer over the actual session messages); do the same in
      // the web stats line by patching the just-emitted stats item
      // with the corrected number.
      if (m.type === "run_completed" && !m.parent_run_id && m.run_id) {
        // Tail drained — drop the "finalizing" indicator unless a new
        // run has already started (which clears it on its own).
        setFinalizing(false);
        const runId = m.run_id;
        void client
          .rpc<number>("count_context_tokens")
          .then((ctx) => {
            if (typeof ctx === "number" && ctx > 0) {
              setItems((prev) => correctStatsCtx(prev, runId, ctx));
              // The same RPC latches the corrected ctx into the BE's
              // _last_input_tokens. Refresh the status payload so the
              // footer meter swaps from Agno's inflated wire number to
              // the corrected one immediately, instead of waiting for
              // the next 5s poll tick.
              void refreshStatus();
            }
          })
          .catch(() => {
            /* leave the un-corrected (inflated) number — better than nothing */
          });
      }
    },
    [client, refreshStatus, setProc, addToast],
  );

  // Persisted history + interrupted-message markers for a session,
  // as renderable items. Used on initial attach AND session picks.
  const fetchHistoryItems = useCallback(
    async (id: string): Promise<ChatItem[]> => {
      const loaded: ChatItem[] = [];
      try {
        const history = await client.rpc<Record<string, unknown>[]>(
          "get_chat_history",
          { session_id: id },
        );
        for (const turn of history || []) {
          const role = String(turn.role ?? "");
          const content = String(turn.content ?? "");
          const runId = typeof turn.run_id === "string" ? turn.run_id : "";
          const item = restoredItem(role, content);
          if (item) {
            // Attach the BE-side run_id to user items so they're
            // edit/delete-targetable after a session restore.
            if (item.kind === "user" && runId) item.runId = runId;
            loaded.push(item);
          }
        }
      } catch {
        /* no history — fresh session */
      }
      try {
        const pending = await client.rpc<Record<string, unknown>[]>(
          "get_pending_messages",
          { session_id: id },
        );
        if (pending?.length) {
          for (const p of pending) {
            // Pending stores the post-process prompt (with the
            // <attached-files> wrapper). Route through restoredItem
            // so the bubble displays only the user-typed text; the
            // @<path> tokens render as inline pills.
            const item = restoredItem("user", String(p.content ?? ""));
            if (item) loaded.push(item);
          }
          loaded.push(
            infoItem(
              `${pending.length} message(s) above were interrupted before completion — the agent knows and can pick up where it left off.`,
            ),
          );
        }
      } catch {
        /* crash recovery is best-effort */
      }
      return loaded;
    },
    [client],
  );

  const refreshSessions = useCallback(async () => {
    try {
      // BE wraps the rows: {type: "session_list_result", sessions: [...]}.
      const res = await client.rpc<{ sessions?: Record<string, unknown>[] }>("list_sessions");
      const list = Array.isArray(res) ? (res as Record<string, unknown>[]) : (res?.sessions ?? []);
      setSessions(
        list.map((s) => {
          const id = String(s.session_id ?? s.id ?? "");
          const ts = Number(s.updated_at ?? s.created_at ?? 0);
          const when = ts
            ? new Date(ts * 1000).toLocaleString([], {
                month: "short",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              })
            : "";
          // Auto-naming runs after the first turn; until then a
          // readable timestamp beats a hex id.
          return {
            session_id: id,
            name: String(s.name ?? "") || (when ? `Chat · ${when}` : id),
            detail: [id, when].filter(Boolean).join(" · "),
          };
        }),
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
            // Hydrate per-client state from the BE before any
            // session-binding decisions — the stored session-id is
            // what tells us whether to attach to an existing session
            // or fall through to the BE's default.
            await clientState.hydrate();
            const storedSidebar = clientState.get(SIDEBAR_KEY);
            if (storedSidebar === "true") setSidebarOpenState(true);
            else if (storedSidebar === "false") setSidebarOpenState(false);
            // Adopt a session for this view. Priority:
            //   1. Already-bound sessionId on the client (reconnect
            //      mid-session — don't churn it).
            //   2. Stored session id from a previous load — re-attach
            //      so the BE pool resumes that session in its
            //      registered directory.
            //   3. BE's default session.
            if (!client.sessionId) {
              const stored = clientState.get(SESSION_KEY);
              if (stored) {
                try {
                  const res = await client.rpc<{
                    session_id: string;
                    project_dir: string;
                  }>("attach_session", { session_id: stored });
                  client.sessionId = res.session_id;
                  setProjectDir(res.project_dir);
                } catch {
                  // Stored id is unusable — fall through to default.
                  client.sessionId = await client.rpc<string>("get_session_id");
                }
              } else {
                client.sessionId = await client.rpc<string>("get_session_id");
              }
            }
            clientState.set(SESSION_KEY, client.sessionId);
            setSessionId(client.sessionId);
            // Resumed BE session (--resume-session / crash restart):
            // show its history instead of an empty welcome.
            const loaded = await fetchHistoryItems(client.sessionId);
            if (loaded.length) setItems(loaded);
          } catch {
            /* ignore */
          }
          // Recompute the real session-context size before fetching
          // status — the BE's ``_last_input_tokens`` may still be the
          // inflated value latched by ``RunCompleted.input_tokens``
          // from a turn that pre-dated the count_context_tokens
          // overwrite fix. ``count_context_tokens`` re-tokenises the
          // actual session messages and writes the corrected number
          // back into ``_last_input_tokens``, so the immediately
          // following ``refreshStatus`` reads the right value into
          // the footer instead of waiting for the next run.
          try {
            await client.rpc<number>("count_context_tokens");
          } catch {
            /* tokenizer not ready / older BE — fall through */
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
          // Silent startup check — populates the version chip and
          // surfaces an OS notification + modal sheet if a newer
          // build is available. The same helper is wired to the
          // native "Check for Updates…" menu item, called with
          // ``silent=false`` so the user gets feedback either way.
          void checkForUpdate(true);
        })();
      }
    });
    const offEvent = client.onEvent((m) => {
      // ── Mirroring events ────────────────────────────────────────
      if (m.type === "welcome") return; // client stored its id
      if (m.type === "typing") {
        // No remote-typing indicator — dropped per user request.
        return;
      }
      if (m.type === "user_message_received") {
        // Another view submitted — paint its bubble here. Our own
        // echo is skipped (we already painted on submit).
        if (m.client_id !== client.clientId) {
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
          // Background processes complete asynchronously — they aren't
          // tied to a specific position in the conversation, so we
          // route them to a toast instead of injecting an info item
          // between chat turns. Same pattern as scheduler events
          // below. (Inline info items here used to split the
          // team-progress card in two when one landed mid-team-run —
          // the orchestrate_event router checked ``last item``.)
          const p = m.payload as {
            cmd?: string;
            exit_code?: number;
            duration_seconds?: number;
          };
          const cmd = String(p.cmd ?? "");
          const cmdShort = cmd.length > 160 ? cmd.slice(0, 159) + "…" : cmd;
          const ok = p.exit_code === 0;
          const dur =
            typeof p.duration_seconds === "number"
              ? ` · ${p.duration_seconds.toFixed(1)}s`
              : "";
          const title = ok
            ? `Background process finished${dur}`
            : `Background process failed (exit ${p.exit_code})${dur}`;
          void notifyHost({
            title,
            body: cmdShort,
            data: { channel: m.channel },
          });
        } else if (
          m.channel === "scheduler_started" ||
          m.channel === "scheduler_completed"
        ) {
          // Scheduled tasks are project-scoped, not chat-scoped — they
          // don't belong in whichever conversation the user happens to
          // be in. Render as a toast (top-right, persistent across
          // conversation switches) and, if the app is backgrounded,
          // additionally fire a native host notification (Tauri /
          // VSCode / JetBrains) so the user notices.
          const desc = String(m.payload.description ?? "").trim() || "(no description)";
          const result = String(m.payload.result ?? "").trim();
          const title =
            m.channel === "scheduler_started"
              ? `⏱ Scheduled task started`
              : `⏱ Scheduled task completed`;
          const body =
            m.channel === "scheduler_completed" && result
              ? `${desc}\n\n${result.length > 240 ? result.slice(0, 240) + "…" : result}`
              : desc;
          void notifyHost({
            title,
            body,
            onClick: () => setPanel({ kind: "schedule" }),
            data: { channel: m.channel, task_id: m.payload.task_id },
          });
        } else if (m.channel === "file_edited") {
          // BE edit_file / edit_file_replace_all / create_file just
          // wrote to disk. Forward to the host (JetBrains for now)
          // so it can refresh the VFS — that one call drops the
          // "modified externally" prompt, reloads any open editor
          // tab, and snapshots a Local History entry. No-ops on
          // hosts without an editor (web, Tauri's plain window).
          const path = String((m.payload as { path?: unknown }).path ?? "");
          if (path) void host.notifyFileEdited(path);
        } else if (m.channel === "orchestrate_event") {
          // Structured tree-event from orchestrate.py. The BE stamps a
          // stable ``card_id`` on every event of a single ``spawn_team``
          // / ``spawn_agent`` invocation — we route by id so the card
          // survives interleaved info items, page refreshes, and
          // concurrent spawns. If we've never seen this card_id, push
          // a fresh card and seed it with the event. Older BEs without
          // card_id fall back to "last active orchestrate item" so a
          // version mismatch still renders something.
          const ev = m.payload as unknown as OrchestrateEvent;
          const cardId = String(
            (m.payload as { card_id?: unknown }).card_id ?? "",
          );
          setItems((prev) => {
            let idx = -1;
            if (cardId) {
              idx = prev.findIndex(
                (it) => it.kind === "orchestrate" && it.cardId === cardId,
              );
            }
            if (idx < 0 && !cardId) {
              // Legacy BE — fall back to the most recent active card.
              for (let i = prev.length - 1; i >= 0; i--) {
                const it = prev[i];
                if (
                  it.kind === "orchestrate" &&
                  isOrchestrateActive(it.agents, it.order)
                ) {
                  idx = i;
                  break;
                }
              }
            }
            if (idx >= 0) {
              const target = prev[idx];
              if (target.kind !== "orchestrate") return prev;
              const { agents, order } = applyOrchestrateEvent(
                target.agents,
                target.order,
                ev,
              );
              // ``applyOrchestrateEvent`` returns the same refs when
              // nothing changed (dedupped content_preview). Skip the
              // re-render — fast-streaming teams fire ~20 events/sec
              // and a no-op state replacement freezes the composer.
              if (agents === target.agents && order === target.order) return prev;
              const next = prev.slice();
              next[idx] = { ...target, agents, order };
              return next;
            }
            const { agents, order } = applyOrchestrateEvent({}, [], ev);
            return [
              ...prev,
              {
                kind: "orchestrate",
                id: Date.now(),
                cardId,
                agents,
                order,
                streaming: true,
              },
            ];
          });
        } else if (m.channel === "orchestrate_progress") {
          // Legacy text channel — kept for backward compat in case a
          // path in orchestrate.py still emits strings. Wraps the
          // line as a content_preview against the "root" agent path
          // so it shows up in the same card without ASCII art. No
          // card_id is carried — fold into the most recent active
          // card, or open a fresh one with an empty cardId.
          const line = String(m.payload.line ?? "").trim();
          if (!line) return;
          const ev: OrchestrateEvent = {
            type: "content_preview",
            agent_path: "root",
            text: line.slice(0, 120),
          };
          setItems((prev) => {
            let idx = -1;
            for (let i = prev.length - 1; i >= 0; i--) {
              const it = prev[i];
              if (
                it.kind === "orchestrate" &&
                isOrchestrateActive(it.agents, it.order)
              ) {
                idx = i;
                break;
              }
            }
            if (idx >= 0) {
              const target = prev[idx];
              if (target.kind !== "orchestrate") return prev;
              const { agents, order } = applyOrchestrateEvent(
                target.agents,
                target.order,
                ev,
              );
              if (agents === target.agents && order === target.order) return prev;
              const next = prev.slice();
              next[idx] = { ...target, agents, order };
              return next;
            }
            const { agents, order } = applyOrchestrateEvent({}, [], ev);
            return [
              ...prev,
              {
                kind: "orchestrate",
                id: Date.now(),
                cardId: "",
                agents,
                order,
                streaming: true,
              },
            ];
          });
        } else if (m.channel === "session_named") {
          void refreshSessions();
        }
      } else if (m.type === "streaming_done" || m.type === "stream_end") {
        // A run initiated by ANOTHER view finished its content.
        setProc(false);
        setFinalizing(true);
      } else if (m.type === "run_completed") {
        // Cross-view tail done — release the finalizing indicator.
        setFinalizing(false);
        setItems((prev) => applyEvent(prev, m, streamRef.current));
      } else if (m.type === "run_started") {
        // A run initiated by another view began — reflect busy state
        // so submits from this view queue instead of racing. A fresh
        // run wipes any leftover tail indicator from the prior one.
        setProc(true);
        setFinalizing(false);
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

  // Autoscroll that respects user intent: stick to the bottom while
  // the user is already there (within a small slack zone), release
  // the lock the moment they scroll away. A floating "↓" button
  // appears in the gap so they can jump back.
  const [stickToBottom, setStickToBottom] = useState(true);
  const SLACK_PX = 80;
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      setStickToBottom(distance <= SLACK_PX);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    if (!stickToBottom) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items, processing, stickToBottom]);
  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setStickToBottom(true);
  }, []);

  // ── Loop continuation (TUI parity: refire after each run) ────────
  const continueLoopIfPending = useCallback(async () => {
    try {
      const next = await client.rpc<{ prompt?: string } | null>(
        "pop_pending_loop_iteration",
      );
      if (next?.prompt) {
        // The BE wraps each iteration's prompt with autonomous-loop
        // instructions; the chat panel renders a tidy iteration card
        // (badge + the user's actual ask) instead of dumping the
        // wrapper text verbatim. The full wrapped prompt is still
        // sent to the model below.
        append(loopItem(next.prompt));
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
      const gen = viewGenRef.current;
      try {
        await client.runMessage(text, (m) => {
          if (gen === viewGenRef.current) onStreamEvent(m);
        });
      } catch (e) {
        append(errorItem(String(e)));
      } finally {
        setProc(false);
        void refreshStatus(); // keep the ctx counter live after each run
        notifyDone();
        void continueLoopIfPending();
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, onStreamEvent],
  );

  // ── Edit / delete a previous user message ────────────────────────
  // Truncates BE session history at the given run, then either stops
  // (delete) or re-runs the new text (edit).
  const truncateAndTrim = useCallback(
    async (target: Extract<ChatItem, { kind: "user" }>): Promise<boolean> => {
      if (!target.runId || !sessionId) return false;
      try {
        const r = await client.rpc<{ removed?: number; error?: string }>(
          "truncate_history",
          { session_id: sessionId, run_id: target.runId },
        );
        if (r?.error) {
          append(errorItem(`Couldn't edit/delete: ${r.error}`));
          return false;
        }
      } catch (e) {
        append(errorItem(`Couldn't edit/delete: ${e instanceof Error ? e.message : String(e)}`));
        return false;
      }
      // Local trim: keep everything strictly BEFORE the target item.
      setItems((prev) => {
        const idx = prev.findIndex((p) => p.id === target.id);
        return idx === -1 ? prev : prev.slice(0, idx);
      });
      // The latched ctx number was for the old (longer) history;
      // grab a fresh status so the footer meter reflects the trim.
      void refreshStatus();
      return true;
    },
    [client, sessionId, append, refreshStatus],
  );

  const onDeleteUser = useCallback(
    async (target: Extract<ChatItem, { kind: "user" }>) => {
      await truncateAndTrim(target);
    },
    [truncateAndTrim],
  );

  const onEditUser = useCallback(
    async (target: Extract<ChatItem, { kind: "user" }>, newText: string) => {
      const ok = await truncateAndTrim(target);
      if (!ok) return;
      // Mirror the typed-submit path: optimistic user item, then run.
      append(userItem(newText));
      await runUserMessage(newText);
    },
    [append, runUserMessage, truncateAndTrim],
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
            viewGenRef.current++;
            setItems([]);
            // The session id rotated and the new conversation has 0
            // context; pull a fresh StatusUpdate so the footer
            // doesn't keep showing the prior session's count.
            void refreshStatus();
            try {
              // /clear renews the runtime's session id — rebind so
              // this view follows the fresh conversation.
              const renewed = await client.rpc<string>("get_session_id");
              client.sessionId = renewed;
              clientState.set(SESSION_KEY, renewed);
              setSessionId(renewed);
            } catch {
              /* ignore */
            }
            // No info line — an empty item list renders the welcome hero.
            void refreshSessions();
            return;
          }
          case "sessions":
            setSidebarOpen(true);
            void refreshSessions();
            return;
          case "model":
            // One picker UI: the composer's model menu (next to Send).
            setModelMenuSignal({ n: Date.now() });
            return;
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
            setPanel({ kind: "agents" });
            return;
          case "skills":
            setPanel({ kind: "skills" });
            return;
          case "plugins":
            setPanel({ kind: "plugins" });
            return;
          case "knowledge":
            setPanel({ kind: "knowledge" });
            return;
          case "hooks":
            setPanel({ kind: "hooks" });
            return;
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
            // Two sources land here:
            //  - Skills expand to a bare prompt → render nothing extra,
            //    just hand it to the run path.
            //  - ``/loop`` iteration 1 — the BE puts the wrapped prompt
            //    in ``result.content`` (the bare ask is in
            //    ``display_content``). Detect the wrapper and paint
            //    the same iteration card the live/restore paths use
            //    so the chat shows "↻ Iteration 1" instead of nothing.
            if (
              typeof result.content === "string" &&
              result.content.startsWith("<loop-iteration ")
            ) {
              append(loopItem(result.content));
              await runUserMessage(result.content);
              return;
            }
            if (content) await runUserMessage(content);
            return;
          case "compact": {
            // BE packs status into ``content`` and the model-generated
            // summary into ``display_content`` (see _cmd_compact). The
            // compact card surfaces both with proper markdown.
            const status = String(result.content ?? "Context compacted.");
            const summary = String(result.display_content ?? "");
            append(compactItem(status, summary));
            // The context just shrunk — refresh the footer meter.
            void refreshStatus();
            return;
          }
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
      const gen = viewGenRef.current;
      try {
        await client.resolveHitlBatch(decisions, (m) => {
          if (gen === viewGenRef.current) onStreamEvent(m);
        });
      } catch (e) {
        append(errorItem(String(e)));
      } finally {
        setProc(false);
        void refreshStatus(); // keep the ctx counter live after each run
        notifyDone();
        void continueLoopIfPending();
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, onStreamEvent],
  );

  const attachInFolder = useCallback(
    async (dir: string) => {
      setPanel({ kind: "none" });
      try {
        // Pool-level attach: creates a fresh session whose tools and
        // $-shell run in that directory; the binding persists with
        // the session (global session→dir registry on the BE).
        const res = await client.rpc<{ session_id: string; project_dir: string }>(
          "attach_session",
          { project_dir: dir },
        );
        client.sessionId = res.session_id;
        clientState.set(SESSION_KEY, res.session_id);
        setSessionId(res.session_id);
        setProjectDir(res.project_dir);
        setItems([]);
        append(infoItem(`Session locked to ${res.project_dir}`));
        void refreshSessions();
        void refreshStatus();
      } catch (e) {
        append(errorItem(String(e)));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client],
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
        clientState.set(SESSION_KEY, id);
        setSessionId(id);
        setItems([]);
        append(infoItem(`Loading session ${id}…`));
        const loaded = await fetchHistoryItems(id);
        setItems([...loaded, infoItem(`Resumed session ${id}.`)]);
        void refreshStatus();
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
        {/* Window drag in the Tauri shell. Three layers of fallback —
            we want this to work on every Tauri build / WebKit quirk:
              1. ``data-tauri-drag-region`` attribute (Tauri's preferred
                 v2 path; missed by some WebKit builds when the host is
                 absolutely positioned with no opaque background).
              2. CSS ``-webkit-app-region: drag`` (in theme.css; same
                 caveat as above).
              3. Explicit ``onMouseDown`` that calls
                 ``getCurrentWindow().startDragging()`` — bypasses
                 both hit-test paths and works regardless. We bail
                 on interactive elements (buttons, inputs, anchors,
                 chips, pills) so they keep their click behaviour.
            All three are no-ops in non-Tauri hosts (the data
            attribute is ignored, the CSS is gated by
            ``[data-host="tauri"]``, and the JS branch checks the
            global before calling). */}
        <header
          ref={headerRef}
          className="app-header"
          data-tauri-drag-region
        >
          {/* Progressive blur stack — six stacked backdrop-filter
              layers, each with a stronger blur and a shorter gradient
              mask, build up a smooth top→bottom blur ramp so messages
              that scroll up behind the header appear frosted. Sits
              absolutely behind the header content via DOM order (the
              interactive children below are painted on top). */}
          <div className="app-header-blur" aria-hidden="true">
            <div className="app-header-blur-layer" />
            <div className="app-header-blur-layer" />
            <div className="app-header-blur-layer" />
            <div className="app-header-blur-layer" />
            <div className="app-header-blur-layer" />
            <div className="app-header-blur-layer" />
          </div>
          <button
            className="icon-btn"
            title="Toggle sessions"
            onClick={() => setSidebarOpen(!sidebarOpen)}
          >
            <MenuIcon />
          </button>
          {/* Brand always renders in the main column's app-header.
              When the sidebar is open it still has its own internal
              "Sessions" section title, but the app identity belongs
              to the main pane so the user's eye anchors there. */}
          <div className="brand">
            <FlameIcon size={20} />
            <span>Ember Code</span>
            <span
              className={`dot ${conn === "replaced" ? "disconnected" : conn}`}
              title={`backend ${conn}`}
              style={{ cursor: "default" }}
            />
          </div>
          <div className="header-spacer" />
          {/* Project chip — hidden when running inside an IDE (VSCode /
              JetBrains): the IDE's workspace folder IS the project root,
              and the FE shouldn't offer a switcher that would force the
              user to override the IDE's notion of "the current project". */}
          {host.kind !== "vscode" && host.kind !== "jetbrains" && (
            <button
              className="chip"
              title={
                projectDir
                  ? `Session locked to ${projectDir} — click to lock a different project`
                  : "Lock this session to a project folder"
              }
              onClick={() =>
                void (async () => {
                  // 1. Shell-injected native dialog (Tauri/IDE webviews).
                  const native = await pickNativeDirectory(projectDir);
                  if (native) return void attachInFolder(native);
                  if (native === null) return; // user cancelled
                  // 2. BE-side native dialog: the BE runs on the user's
                  //    machine, so it can open the real OS picker even
                  //    for a plain browser tab. 10-min timeout — the
                  //    dialog waits for a human.
                  try {
                    const res = await client.rpc<{
                      path: string;
                      cancelled: boolean;
                      error: string;
                    }>("pick_dir_native", {}, 600_000);
                    if (res.path) return void attachInFolder(res.path);
                    if (res.cancelled) return;
                    // fall through on error (headless Linux, etc.)
                  } catch {
                    /* fall through to in-app picker */
                  }
                  // 3. Last resort: in-app server-side browser.
                  setPanel({ kind: "dir-picker" });
                })()
              }
            >
              <FolderIcon />{" "}
              <span
                className="chip-label"
                style={{
                  // Full path, truncated from the LEFT so the project
                  // name (the informative end) stays visible.
                  maxWidth: 380,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  direction: "rtl",
                  textAlign: "left",
                }}
              >
                {projectDir ? `⁨${projectDir}⁩` : "project"}
              </span>
            </button>
          )}
          {status?.cloud_connected ? (
            <button
              className="chip"
              title="Account"
              onClick={() => setAccountMenu(!accountMenu)}
            >
              <CloudIcon /> <span className="chip-label">{status.cloud_org}</span> <ChevronIcon size={9} down />
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
          {conn === "replaced" && (
            <button
              className="chip"
              title="This chat was opened in another tab/window. Click to take over here."
              onClick={() => client.connect()}
            >
              <span className="dot disconnected" />
              <span className="chip-label">opened elsewhere — reconnect</span>
            </button>
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

        {/* Custom scroll-position indicator — the native scrollbar is
            hidden because it would render inside the ``.conversation``
            layer (z:1) and get blurred by the header. Placed as a
            sibling of ``.conversation`` (not a child) so it doesn't
            scroll with the content; positioned absolutely against the
            ``.main`` column at z:30, above the header blur. */}
        <ScrollIndicator scrollRef={scrollRef} />
        <div className="conversation" ref={scrollRef}>
          <div className={`col${processing ? " streaming" : ""}`}>
            {items.length === 0 && (
              <div className="welcome">
                <div style={{ display: "flex", justifyContent: "center" }}>
                  <FlameIcon size={56} />
                </div>
                <h1>Ember Code</h1>
                <p>Your AI coding agent, in this project.</p>
                <div className="welcome-caps">
                  {(
                    [
                      ["/agents", "Dispatch to a specialist — architect, debugger, …"],
                      ["/skills", "Workflows like /commit and /resolve-issues"],
                      ["/codeindex", "Semantic search across your repo"],
                      ["/schedule", "Background tasks that report back"],
                      ["/loop", "Repeat a prompt across a batch until done"],
                      ["/mcp", "Plug in external tools and data sources"],
                      ["/plugins", "Install skills, agents and hooks"],
                      ["/knowledge", "A knowledge base carried in git"],
                    ] as const
                  ).map(([cmd, desc]) => (
                    <div
                      key={cmd}
                      className="welcome-cap"
                      onClick={() => void runCommand(cmd, false)}
                    >
                      <code>{cmd}</code>
                      <span>{desc}</span>
                    </div>
                  ))}
                </div>
                <p style={{ color: "var(--fg-faint)", fontSize: 12.5, marginTop: 22 }}>
                  Enter to send · Shift+Enter newline · / commands · @ files · $ shell
                </p>
              </div>
            )}
            {items.map((item) => (
              <ChatItemView
                key={item.id}
                item={item}
                onEditUser={onEditUser}
                onDeleteUser={onDeleteUser}
                onStopTeam={() => client.cancel()}
                onStopAgent={(runId) =>
                  void client
                    .rpc("cancel_agent_run", { run_id: runId })
                    .catch((err) => console.warn("cancel_agent_run failed", err))
                }
                onRetryAgent={(agentName, newTask) => {
                  // Send a follow-up user message asking the main
                  // agent to respawn the specialist with the
                  // (possibly tweaked) task. Free-form text — the
                  // model can interpret context, decide which mode
                  // to use, etc.
                  const msg =
                    `Please retry the ${agentName} sub-agent with this task:` +
                    `\n\n${newTask}`;
                  void runUserMessage(msg);
                }}
              />
            ))}
            {(processing || finalizing) && (
              <div className="typing-indicator" aria-live="polite">
                <span className="typing-dots">
                  <span />
                  <span />
                  <span />
                </span>
                <span className="typing-label">
                  {processing ? "Ember is replying…" : "Finalizing…"}
                </span>
                {processing && <span className="typing-hint">Esc to cancel</span>}
              </div>
            )}
            {!stickToBottom && (
              <button
                type="button"
                className="scroll-to-bottom"
                title="Scroll to latest"
                aria-label="Scroll to latest"
                onClick={scrollToBottom}
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 16 16"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M8 3v9.5M3.8 8.3L8 12.5l4.2-4.2" />
                </svg>
              </button>
            )}
          </div>
        </div>

        <Composer
          hitlSlot={
            hitl ? (
              <HitlDialog requirements={hitl} onResolve={(d) => void resolveHitl(d)} />
            ) : null
          }
          client={client}
          connected={conn === "connected"}
          processing={processing}
          skills={skills}
          tools={TOOLS_MENU}
          seed={composerSeed}
          sessionId={sessionId}
          clientState={clientState}
          model={status?.model}
          modelMenuSignal={modelMenuSignal}
          onPickModel={(name) => void pickModel(name)}
          onTool={(cmd) => void runCommand(cmd, false)}
          onSubmit={(t) => void submit(t)}
          onStop={() => client.cancel()}
        />
        <div className="statusline" style={{ marginTop: 0, paddingBottom: 8 }}>
          {status && (
            <>
              <SessionChip sessionId={sessionId} />
              <CtxMeter
                tokens={status.context_tokens}
                max={status.max_context}
                pct={ctxPct}
              />
              <CodeIndexIndicator
                client={client}
                onOpen={() => setPanel({ kind: "codeindex" })}
              />
            </>
          )}
          {pendingUpdate && (
            <button
              type="button"
              className="brand-update"
              title={`Update to v${pendingUpdate.latest_version}`}
              onClick={() => setShowUpdateModal(true)}
            >
              Update to v{pendingUpdate.latest_version}
            </button>
          )}
        </div>
      </div>

      {panel.kind === "mcp" && (
        <McpPanel
          client={client}
          onClose={() => setPanel({ kind: "none" })}
          onAddServer={(seed) => {
            setPanel({ kind: "none" });
            setComposerSeed({ text: seed, n: Date.now() });
          }}
        />
      )}
      {panel.kind === "codeindex" && (
        <CodeIndexPanel client={client} onClose={() => setPanel({ kind: "none" })} />
      )}
      {panel.kind === "loop" && (
        <LoopPanel
          client={client}
          onClose={() => setPanel({ kind: "none" })}
          onResume={(prompt) => {
            // Close the panel so the user can watch the iteration
            // stream in the chat — same UX as the slash-command path
            // for /loop resume.
            setPanel({ kind: "none" });
            append(loopItem(prompt));
            void runUserMessage(prompt);
          }}
        />
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
      {panel.kind === "agents" && (
        <AgentsPanel client={client} onClose={() => setPanel({ kind: "none" })} />
      )}
      {panel.kind === "skills" && (
        <SkillsPanel
          client={client}
          onRun={(cmd) => {
            // Don't fire the skill — drop it into the composer so the
            // user can add arguments and send when ready.
            setPanel({ kind: "none" });
            // No trailing space — keeps the autocomplete menu open so
            // the user can pick a sub-suggestion or just press Enter.
            setComposerSeed({ text: cmd, n: Date.now() });
          }}
          onClose={() => setPanel({ kind: "none" })}
        />
      )}
      {panel.kind === "plugins" && (
        <PluginsPanel client={client} onClose={() => setPanel({ kind: "none" })} />
      )}
      {panel.kind === "knowledge" && (
        <KnowledgePanel client={client} onClose={() => setPanel({ kind: "none" })} />
      )}
      {panel.kind === "hooks" && (
        <HooksPanel client={client} onClose={() => setPanel({ kind: "none" })} />
      )}
      {previewPath && (
        <FilePreview
          client={client}
          path={previewPath}
          onClose={() => setPreviewPath(null)}
        />
      )}
      <Toasts items={toasts} onDismiss={dismissToast} />
      {showUpdateModal && pendingUpdate && (
        <UpdatePrompt
          info={pendingUpdate}
          onDismiss={() => setShowUpdateModal(false)}
        />
      )}
      {panel.kind === "dir-picker" && (
        <DirectoryPicker
          client={client}
          title="Lock session to a project folder"
          initialPath={projectDir}
          onSelect={(dir) => void attachInFolder(dir)}
          onCancel={() => setPanel({ kind: "none" })}
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
