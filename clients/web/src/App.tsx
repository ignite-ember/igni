import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applyEvent,
  applyOrchestrateEvent,
  assistantItem,
  compactItem,
  correctStatsCtx,
  restoredItem,
  restoredStatsItem,
  errorItem,
  infoItem,
  isOrchestrateActive,
  loopItem,
  mergePlanTasks,
  newStreamState,
  normalizePlanTasks,
  planItem,
  shellItem,
  userItem,
  visualizationItem,
  type ChatItem,
  type OrchestrateEvent,
} from "./chat/model";
import { applySpecPatch, type JsonPatch, type Spec } from "@json-render/core";
import { nextObserverBusyState } from "./chat/observerBusy";
import { ClientStateStore, ensureClientId } from "./clientState";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { ChatItemView } from "./components/ChatItems";
import { ChatSearchBar } from "./components/ChatSearchBar";
import { Composer, BUILTIN_COMMANDS, type SlashCommand } from "./components/Composer";
import { CodeIndexIndicator } from "./components/CodeIndexIndicator";
import { WatcherIndicator } from "./components/WatcherIndicator";
import { BackendVersionChip, CtxMeter, SessionChip } from "./components/StatusBits";
import { FPSCounterOverlay } from "./components/FPSCounter";
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
import { WatcherPanel } from "./components/panels/WatcherPanel";
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
  | { kind: "watcher" }
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
  { label: "Watcher", command: "/watcher", desc: "background processes & live logs" },
  { label: "Compact context", command: "/compact", desc: "summarize old turns" },
  { label: "Context breakdown", command: "/ctx", desc: "system vs runs token split" },
  { label: "Help", command: "/help", desc: "all commands" },
];

/** Walk back from a ``stats`` item's index to find the assistant
 *  text that closes the same run. The inline copy-response button on
 *  the stats line copies this text. We stop at the previous user
 *  message because every stats item belongs to exactly one turn. */
function findAssistantTextForStats(items: ChatItem[], idx: number): string | undefined {
  const stats = items[idx];
  if (!stats || stats.kind !== "stats") return undefined;
  for (let i = idx - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "user") return undefined;
    if (it.kind === "assistant") return it.text;
  }
  return undefined;
}

/** Pretty label for ``Tier`` values from ember-server
 *  (lite / pro / max / codeindex). The DB stores lowercase, the UI
 *  wants title-case; ``codeindex`` is the special-purpose CodeIndex-
 *  only tier so we spell it out for clarity. */
function formatPlanName(tier: string): string {
  switch (tier.toLowerCase()) {
    case "lite":
      return "Lite";
    case "pro":
      return "Pro";
    case "max":
      return "Max";
    case "codeindex":
      return "CodeIndex";
    default:
      return tier;
  }
}

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
          setHistoryIndexToItemIndex([]);
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
        // Match the host's actual panel background so the tool
        // window doesn't paint a differently-shaded patch inside
        // Darcula / High Contrast / custom themes. ``--bg`` is the
        // one custom property every panel / composer / dialog
        // sources their background from; overriding it as an
        // inline style on ``:root`` beats the CSS-file default
        // via specificity.
        const bg = typeof payload.bg === "string" ? payload.bg : null;
        if (bg) {
          document.documentElement.style.setProperty("--bg", bg);
        } else {
          document.documentElement.style.removeProperty("--bg");
        }
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
          body: `igni ${info.latest_version} is ready to install.`,
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
        "https://github.com/ignite-ember/igni/releases/latest",
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
  // Plan tier (``lite`` / ``pro`` / ``max`` / ``codeindex``) fetched
  // on demand whenever the account popover opens — the BE calls
  // ``/portal/me`` on the cloud and returns the tier from the user's
  // active org membership. Refreshing on each open ensures the UI
  // reflects seat-tier reassignments without an app restart.
  const [cloudPlan, setCloudPlan] = useState<string | null>(null);
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
        new Notification("igni", { body: "Your reply is ready.", silent: true });
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

  // Tracks an acceptEdits flip that was triggered by the HITL
  // dialog's *"Accept all edits during this session"* shortcut. The
  // mental model the user signed up for is "auto-approve edits FOR
  // THIS TASK" — not "permanently lower the gate". When the run's
  // content stream ends (``streaming_done``) we revert by firing
  // ``/accept off`` so the next user turn starts in default mode.
  const autoAcceptForRunRef = useRef(false);

  // Latch the current top-level run_id so ``visualization`` push
  // handlers can associate the saved card with the run that
  // produced it — the BE positions restored visualizations right
  // after the last turn of the matching run. Cleared on
  // ``run_completed`` so a stale id doesn't attach to a later
  // orphan push. See ``get_chat_history``'s visualization splice.
  const currentRunIdRef = useRef<string>("");

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
        // Revert the auto-accept-for-this-run flip the shortcut put
        // in place. Runs ``/accept off`` silently so the chat list
        // doesn't show a fake typed slash command.
        if (autoAcceptForRunRef.current) {
          autoAcceptForRunRef.current = false;
          void runCommand("/accept off", false);
        }
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
        // Latch the top-level run_id (no parent) so visualization
        // saves can associate cards with the run.
        if (!m.parent_run_id && m.run_id) {
          currentRunIdRef.current = m.run_id;
        }
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
        // Clear the latched run_id so a stale value doesn't
        // attach to any late push. A new run_started will set it.
        if (currentRunIdRef.current === m.run_id) {
          currentRunIdRef.current = "";
        }
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
  // Parallel to ``items``: ``historyIndexToItemIndex[i]`` is the FE
  // items position of the i-th turn in ``get_chat_history``'s output,
  // or -1 if the turn was filtered out by ``restoredItem``. The chat
  // search bar uses this to translate BE match indices into FE
  // scroll-to positions without re-walking the items array.
  const [historyIndexToItemIndex, setHistoryIndexToItemIndex] = useState<number[]>(
    [],
  );

  const fetchHistoryItems = useCallback(
    async (id: string): Promise<{ items: ChatItem[]; historyMap: number[] }> => {
      const loaded: ChatItem[] = [];
      const historyMap: number[] = [];
      try {
        const history = await client.rpc<Record<string, unknown>[]>(
          "get_chat_history",
          { session_id: id },
        );
        // Accumulate visible assistant text per run_id so the stats
        // turn can estimate ``visibleOutTokens`` from the same string
        // the user sees — matching the live path's behavior.
        const assistantTextByRun = new Map<string, string>();
        for (const turn of history || []) {
          const role = String(turn.role ?? "");
          const runId = typeof turn.run_id === "string" ? turn.run_id : "";
          if (role === "stats") {
            historyMap.push(loaded.length);
            loaded.push(restoredStatsItem(turn, assistantTextByRun.get(runId) ?? ""));
            continue;
          }
          const item = restoredItem(turn);
          if (item) {
            // Attach the BE-side run_id to user items so they're
            // edit/delete-targetable after a session restore.
            if (item.kind === "user" && runId) item.runId = runId;
            if (item.kind === "assistant" && runId) {
              const prev = assistantTextByRun.get(runId) ?? "";
              assistantTextByRun.set(runId, prev ? `${prev} ${item.text}` : item.text);
            }
            historyMap.push(loaded.length);
            loaded.push(item);
          } else {
            // ``restoredItem`` filtered this turn out (empty after
            // stripping). Record -1 so the search-result mapping
            // marks this match as unreachable rather than landing on
            // the wrong item.
            historyMap.push(-1);
          }
        }
      } catch {
        /* no history — fresh session */
      }
      // PlanCards are emitted inline by ``get_chat_history`` (a
      // synthetic ``role: "plan"`` turn replaces each
      // ``exit_plan_mode`` tool result) so the card renders at the
      // conversation position where the agent submitted it — not
      // bolted onto the end. No separate fetch needed.
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
            const item = restoredItem({ role: "user", content: String(p.content ?? "") });
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
      return { items: loaded, historyMap };
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
            // show its history instead of an empty welcome. The
            // actual "land at the most recent message" scroll fires
            // from the ``bootScrollDoneRef`` effect below — we
            // can't call ``scrollToBottom`` inline here because
            // ``setItems`` is async and Virtuoso's ``data`` prop
            // hasn't updated yet, so ``scrollToIndex("LAST")``
            // would resolve against the still-empty list.
            const loaded = await fetchHistoryItems(client.sessionId);
            if (loaded.items.length) setItems(loaded.items);
            setHistoryIndexToItemIndex(loaded.historyMap);
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
          // Visualizer streaming path: the visualizer sub-agent emits
          // its whole response as JSONL RFC-6902 patches, which
          // orchestrate.py forwards on this channel as
          // ``{type: "visualization_patch", spec_id, patch}``. Apply
          // each patch to the card's spec state (creating the card on
          // the first patch, updating in place after). No orchestrate
          // team-card touch — visualizer runs go straight to their
          // own card.
          const rawEv = m.payload as {
            type?: unknown;
            spec_id?: unknown;
            patch?: unknown;
          };
          if (
            rawEv.type === "visualization_patch" &&
            typeof rawEv.spec_id === "string" &&
            rawEv.patch &&
            typeof rawEv.patch === "object"
          ) {
            const specId = rawEv.spec_id;
            const patch = rawEv.patch as JsonPatch;
            setItems((prev) => {
              const idx = prev.findIndex(
                (it) => it.kind === "visualization" && it.specId === specId,
              );
              if (idx >= 0) {
                const existing = prev[idx] as Extract<
                  ChatItem,
                  { kind: "visualization" }
                >;
                const base: Spec =
                  (existing.spec as Spec | null) ?? {
                    root: "",
                    elements: {},
                  };
                let nextSpec: Spec;
                try {
                  // applySpecPatch mutates + returns; spread for a
                  // new reference React can shallow-compare.
                  nextSpec = { ...applySpecPatch(base, patch) };
                } catch (e) {
                  // eslint-disable-next-line no-console
                  console.warn("visualization_patch apply failed", patch, e);
                  return prev;
                }
                const next = prev.slice();
                next[idx] = { ...existing, spec: nextSpec };
                return next;
              }
              // First patch for this spec_id — bootstrap the card.
              let seed: Spec = { root: "", elements: {} };
              try {
                seed = { ...applySpecPatch(seed, patch) };
              } catch (e) {
                // eslint-disable-next-line no-console
                console.warn("visualization_patch seed failed", patch, e);
              }
              return [
                ...prev,
                visualizationItem(seed, "", "visualizer", specId),
              ];
            });
            return; // Fully handled; don't fall through to team-card logic
          }

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
        } else if (m.channel === "permission_mode_changed") {
          // BE flipped the live ``PermissionEvaluator.mode``. The
          // status badge reads from ``status.permission_mode``;
          // patch the cached status so the badge updates without
          // waiting for the next ``status_update`` push (which
          // only fires on token-counter changes).
          const mode = String(
            (m.payload as { mode?: unknown }).mode ?? "default",
          );
          setStatus((prev) =>
            prev ? { ...prev, permission_mode: mode } : prev,
          );
          // When the AGENT entered plan mode (via the
          // ``enter_plan_mode`` tool, not the user's /plan
          // command) inject an inline info banner so the user
          // sees what happened and why. The basic flip
          // broadcast lacks ``source`` so we only paint the
          // banner on the follow-up agent-attributed event.
          const src = String((m.payload as { source?: unknown }).source ?? "");
          if (src === "agent" && mode === "plan") {
            const reason = String(
              (m.payload as { reason?: unknown }).reason ?? "",
            ).trim();
            const text = reason
              ? `Agent entered plan mode — ${reason}`
              : "Agent entered plan mode.";
            append(infoItem(text));
          }
        } else if (m.channel === "plan_submitted") {
          // Agent called ``exit_plan_mode(plan, tasks=[...])``.
          // Append a ``plan`` ChatItem so the user sees the
          // plan card + Approve / Refine buttons inline in the
          // conversation. ``tasks`` (optional) seeds the live
          // checklist; ``todos_updated`` pushes refresh the
          // statuses during execution. ``run_id`` is the BE-side
          // key for approve/dismiss RPCs — stamped onto the
          // payload at drain time by the run loop.
          const payload = m.payload as {
            plan?: unknown;
            tasks?: unknown;
            run_id?: unknown;
          };
          const planText = String(payload.plan ?? "").trim();
          const runId = String(payload.run_id ?? "");
          if (planText) {
            append(planItem(planText, normalizePlanTasks(payload.tasks), runId));
          }
        } else if (m.channel === "plan_decided") {
          // Server-of-truth state change. Fired by
          // ``Session.approve_plan`` / ``dismiss_plan`` after
          // the decision is persisted. We update the matching
          // PlanCard by ``runId`` so multiple stacked plans in
          // the chat each flip independently.
          const payload = m.payload as { run_id?: unknown; decision?: unknown };
          const runId = String(payload.run_id ?? "");
          const decision = String(payload.decision ?? "");
          if (
            runId &&
            (decision === "approved" || decision === "dismissed")
          ) {
            setItems((prev) =>
              prev.map((it) =>
                it.kind === "plan" && it.runId === runId
                  ? { ...it, state: decision }
                  : it,
              ),
            );
          }
        } else if (m.channel === "todos_updated") {
          // ``todo_write`` mutated the TodoStore. Patch every open
          // ``plan`` ChatItem's tasks — see ``mergePlanTasks`` for
          // the matching/preserve semantics.
          const todos = (m.payload as { todos?: unknown }).todos;
          setItems((prev) =>
            prev.map((it) =>
              it.kind === "plan"
                ? { ...it, tasks: mergePlanTasks(it.tasks, todos) }
                : it,
            ),
          );
        } else if (m.channel === "visualization") {
          // Visualizer sub-agent emitted a json-render spec via
          // ``VisualizeTools.visualize``. Payload shape:
          // ``{ spec: {...}, spec_id: str, title?: string, source_agent?: string }``.
          //
          // Dedup by ``spec_id``: the visualizer's toolkit generates a
          // stable id per sub-agent run, so repeated calls within a
          // single run UPDATE the same card instead of appending a
          // new one. That's the on-ramp for streaming — one card per
          // stream, updated in place as data arrives.
          const p = m.payload as {
            spec?: unknown;
            spec_id?: unknown;
            title?: unknown;
            source_agent?: unknown;
          };
          const spec = p.spec;
          if (
            spec &&
            typeof spec === "object" &&
            "root" in (spec as Record<string, unknown>) &&
            "elements" in (spec as Record<string, unknown>)
          ) {
            const specId =
              typeof p.spec_id === "string" && p.spec_id ? p.spec_id : "";
            const title = typeof p.title === "string" ? p.title : "";
            const sourceAgent =
              typeof p.source_agent === "string" ? p.source_agent : "visualizer";
            setItems((prev) => {
              // Find an existing visualization card with the same
              // spec_id; if present, replace its spec/title in place.
              const idx = specId
                ? prev.findIndex(
                    (it) => it.kind === "visualization" && it.specId === specId,
                  )
                : -1;
              if (idx >= 0) {
                const next = prev.slice();
                const existing = next[idx] as Extract<
                  ChatItem,
                  { kind: "visualization" }
                >;
                next[idx] = { ...existing, spec, title, sourceAgent };
                return next;
              }
              return [...prev, visualizationItem(spec, title, sourceAgent, specId)];
            });
            // Persist so a session reload restores this card inline
            // via ``get_chat_history``. Idempotent on ``spec_id`` —
            // multiple pushes for the same run just replace the
            // stored entry, not accumulate duplicates. Best-effort:
            // a failed save logs but doesn't break the live render.
            if (specId) {
              void client
                .rpc("save_visualization", {
                  spec_id: specId,
                  spec,
                  title,
                  source_agent: sourceAgent,
                  run_id: currentRunIdRef.current,
                })
                .catch((e) => {
                  // eslint-disable-next-line no-console
                  console.warn("save_visualization failed", e);
                });
            }
          }
        }
      } else {
        // Cross-view busy-indicator update. The state machine —
        // including the ``stream_end`` quirk where it fires after
        // ``run_completed`` and MUST NOT re-arm finalizing — lives
        // in ``chat/observerBusy.ts`` and is exercised by direct
        // reducer tests. ``processingRef`` holds the synchronous
        // current ``proc`` so we can pass an accurate ``prev`` in
        // (only ``run_completed`` actually reads ``prev.proc``;
        // the other transitions produce prev-independent output).
        const next = nextObserverBusyState(
          { proc: processingRef.current, finalizing: false },
          m,
        );
        setProc(next.proc);
        setFinalizing(next.finalizing);
        // Item rendering (content deltas, tool cards, stats line,
        // etc.) is orthogonal to the busy indicator — always apply
        // except for the pure-terminator events that carry no
        // renderable payload.
        if (m.type !== "streaming_done" && m.type !== "stream_end") {
          setItems((prev) => applyEvent(prev, m, streamRef.current));
        }
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
  // Virtuoso owns scroll. We just observe the "at bottom" state to
  // toggle the scroll-to-bottom button. ``followOutput="auto"`` keeps
  // the view glued to the tail whenever the user is already there;
  // when they've scrolled up to read history it leaves the position
  // alone (no jumping mid-read).
  const [stickToBottom, setStickToBottom] = useState(true);
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const [scrollEl, setScrollEl] = useState<HTMLElement | null>(null);
  // Keep the legacy ``scrollRef`` populated so anything still reaching
  // for it (debug, tests) sees the real scroller.
  useEffect(() => {
    scrollRef.current = scrollEl as HTMLDivElement | null;
  }, [scrollEl]);
  const scrollToBottom = useCallback(() => {
    virtuosoRef.current?.scrollToIndex({ index: "LAST", behavior: "auto" });
    setStickToBottom(true);
  }, []);

  // ── Boot scroll: land the resumed session at its most recent
  // message instead of the very first user prompt. Fires once,
  // the first time ``items`` transitions from empty to populated
  // (after React commits + Virtuoso has the new ``data`` prop —
  // calling ``scrollToBottom`` inline from the boot effect runs
  // BEFORE the commit, so ``scrollToIndex("LAST")`` resolves
  // against the still-empty list and lands on nothing).
  const bootScrollDoneRef = useRef(false);
  useEffect(() => {
    if (bootScrollDoneRef.current) return;
    if (items.length === 0) return;
    bootScrollDoneRef.current = true;
    scrollToBottom();
  }, [items.length, scrollToBottom]);
  // Find-in-conversation. Cmd/Ctrl+F opens the bar; a small icon
  // button near the chat header opens it too. ``highlightedItemId``
  // applies a pulse class to the jumped-to message via the new
  // ChatItemView prop so the user's eye lands on it after the scroll.
  const [searchOpen, setSearchOpen] = useState(false);
  const [highlightedItemId, setHighlightedItemId] = useState<number | null>(null);
  const highlightTimerRef = useRef<number | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const cmdLike = e.metaKey || e.ctrlKey;
      if (cmdLike && e.key.toLowerCase() === "f") {
        // Only intercept when there's something to search.
        if (items.length === 0) return;
        e.preventDefault();
        setSearchOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items.length]);
  const onSearchJump = useCallback(
    (itemIndex: number) => {
      virtuosoRef.current?.scrollToIndex({
        index: itemIndex,
        align: "center",
        behavior: "auto",
      });
      const target = items[itemIndex];
      if (target) {
        setHighlightedItemId(target.id);
        if (highlightTimerRef.current !== null)
          window.clearTimeout(highlightTimerRef.current);
        highlightTimerRef.current = window.setTimeout(
          () => setHighlightedItemId(null),
          2400,
        );
      }
    },
    [items],
  );
  useEffect(
    () => () => {
      if (highlightTimerRef.current !== null)
        window.clearTimeout(highlightTimerRef.current);
    },
    [],
  );

  // Memoize the Virtuoso ``components`` map so its identity is
  // stable across App re-renders that don't change the footer
  // state — without this, every state update (token streamed, status
  // ticked) would hand Virtuoso a fresh Footer component and force
  // it to remount the indicator.
  const virtuosoComponents = useMemo(
    () => ({
      // 78 px spacer so the first message's edit/delete chip clears
      // the OS-level drag region of the frosted header. Matches the
      // original ``.conversation > .col { padding-top: 78px }`` rule
      // that lived on the single column wrapper before virtualization.
      Header: () => <div aria-hidden="true" style={{ height: 78 }} />,
      Footer: () =>
        processing || finalizing ? (
          <div className="chat-row">
            <div className="typing-indicator" aria-live="polite">
              <span className="typing-dots">
                <span />
                <span />
                <span />
              </span>
              <span className="typing-label">
                {processing ? "igni is replying…" : "Finalizing…"}
              </span>
              {processing && <span className="typing-hint">Esc to cancel</span>}
            </div>
          </div>
        ) : null,
    }),
    [processing, finalizing],
  );

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

  // Stable per-item callbacks. The `items.map` below passes these to
  // every ChatItemView; if they were inline arrows, React.memo's
  // shallow compare would always miss and every item would re-render
  // on every App state change (status, processing, stream events).
  const onStopTeam = useCallback(() => client.cancel(), [client]);
  const onStopAgent = useCallback(
    (runId: string) =>
      void client
        .rpc("cancel_agent_run", { run_id: runId })
        .catch((err) => console.warn("cancel_agent_run failed", err)),
    [client],
  );
  const onRetryAgent = useCallback(
    (agentName: string, newTask: string) => {
      // Send a follow-up user message asking the main agent to
      // respawn the specialist with the (possibly tweaked) task —
      // free-form text so the model can pick the right mode.
      const msg =
        `Please retry the ${agentName} sub-agent with this task:` +
        `\n\n${newTask}`;
      void runUserMessage(msg);
    },
    [runUserMessage],
  );

  // ── Plan-card actions (row 50) ──────────────────────────────────
  // Approve = call ``approve_plan(run_id)`` on the BE so the
  // decision is persisted (survives reload), then send a brief
  // user message to wake the agent into executing. The BE RPC
  // is what flips ``PermissionEvaluator.mode`` back to default
  // AND emits a ``plan_decided`` push that updates the card's
  // visual state — we don't pre-flip in the FE so the BE stays
  // the single source of truth. The wake-up message is still
  // FE-driven because it's logically a user-typed continuation
  // (it lands in the chat transcript as such).
  // Refine = persist the dismissal via ``dismiss_plan(run_id)``
  // and stay in plan mode so the user can iterate.
  const onApprovePlan = useCallback(
    (id: number) => {
      const card = items.find(
        (it): it is Extract<ChatItem, { kind: "plan" }> =>
          it.kind === "plan" && it.id === id,
      );
      const runId = card?.runId ?? "";
      void (async () => {
        if (runId) {
          try {
            await client.approvePlan(runId);
          } catch (e) {
            console.warn("approve_plan RPC failed", e);
          }
        }
        await runUserMessage("Plan approved — execute it now.");
      })();
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items],
  );
  // Round-trip for interactive components inside a
  // ``<JsonRenderView>`` card (Button click, Select change, etc.).
  // See ``JsonRenderView`` — every named action funnels through the
  // ``handlers`` Proxy to this callback, which RPCs the BE so any
  // agent that cares can observe the event on
  // ``session._visualization_actions``.
  const onDispatchVisualizationAction = useCallback(
    async (action: string, params: Record<string, unknown>) => {
      try {
        return await client.rpc<{ ok: boolean; action: string; params: unknown }>(
          "dispatch_visualization_action",
          { action, params },
        );
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn("dispatch_visualization_action failed", e);
        return undefined;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const onRejectPlan = useCallback(
    (id: number) => {
      const card = items.find(
        (it): it is Extract<ChatItem, { kind: "plan" }> =>
          it.kind === "plan" && it.id === id,
      );
      const runId = card?.runId ?? "";
      if (runId) {
        void client.dismissPlan(runId).catch((e) => {
          console.warn("dismiss_plan RPC failed", e);
        });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items],
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
            setHistoryIndexToItemIndex([]);
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
          case "fork": {
            const newId = content.trim();
            if (!newId) {
              append(errorItem("Fork failed: backend returned no session id"));
              return;
            }
            // Same dance as ``/clear`` but we also rehydrate the
            // cloned history so the fork opens with the same context
            // the user just left in the source session.
            viewGenRef.current++;
            setItems([]);
            setHistoryIndexToItemIndex([]);
            client.sessionId = newId;
            clientState.set(SESSION_KEY, newId);
            setSessionId(newId);
            try {
              const loaded = await fetchHistoryItems(newId);
              setItems(loaded.items);
              setHistoryIndexToItemIndex(loaded.historyMap);
            } catch (e) {
              append(errorItem(`Loaded fork id but history fetch failed: ${e}`));
            }
            append(infoItem("Forked to a new session — continuing the dialogue."));
            void refreshStatus();
            void refreshSessions();
            return;
          }
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
          case "watcher":
            setPanel({ kind: "watcher" });
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
        setHistoryIndexToItemIndex([]);
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
        // Show the first 8 hex chars only — full UUIDs are noisy in
        // an info bubble and the short prefix is uniquely identifying
        // in practice across the sidebar listing.
        const short = id.slice(0, 8);
        append(infoItem(`Loading session ${short}…`));
        const loaded = await fetchHistoryItems(id);
        setItems([...loaded.items, infoItem(`Resumed session ${short}.`)]);
        setHistoryIndexToItemIndex(loaded.historyMap);
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
      {/* Live FPS overlay — hidden by default. Toggle with
          ``Ctrl+Alt+Shift+F`` (or ``Cmd+Alt+Shift+F`` on macOS).
          State persists per-origin via localStorage so the
          overlay stays put across reloads until the user turns
          it off again. Documented in the plugin README so
          contributors verifying rendering-pipeline changes can
          find it, invisible to everyone else. */}
      <FPSCounterOverlay />
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
          {/* Solid opaque panel painted behind the header content
              (``z-index: -1``) so messages scrolling under the
              header disappear at the bottom edge. Also the Tauri
              window-drag hit-area (hangs 28 px past the header). */}
          <div className="app-header-blur" aria-hidden="true" />
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
            <span>igni</span>
            <span
              className={`dot ${conn === "replaced" ? "disconnected" : conn}`}
              title={`backend ${conn}`}
              style={{ cursor: "default" }}
            />
            {/* Version last in the brand row — reads as
                ``igni ● v0.9.0``. Renders only when a host
                embedded the values (JB / Tauri / VSCode); the
                component itself paints nothing otherwise. */}
            <BackendVersionChip />
          </div>
          <div className="header-spacer" />
          {items.length > 0 && (
            <button
              className={`chip chip-search${searchOpen ? " active" : ""}`}
              title="Find in conversation (⌘F)"
              aria-label="Find in conversation"
              onClick={() => setSearchOpen((v) => !v)}
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
                style={{ display: "block" }}
              >
                <circle cx="7" cy="7" r="4.5" />
                <path d="M10.5 10.5L13.5 13.5" />
              </svg>
            </button>
          )}
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
              onClick={() => {
                const opening = !accountMenu;
                setAccountMenu(opening);
                if (opening) {
                  // Fresh fetch every time the popover opens so seat
                  // tier changes show up without an app restart. The
                  // BE proxies to ``/portal/me`` on the cloud.
                  void client
                    .rpc<{ tier?: string | null } | null>("get_cloud_plan")
                    .then((info) => setCloudPlan(info?.tier ?? null))
                    .catch(() => setCloudPlan(null));
                }
              }}
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
                {cloudPlan && (
                  <div className="popup-item" style={{ cursor: "default" }}>
                    <span className="desc">
                      Plan:{" "}
                      <span style={{ color: "var(--fg)", fontWeight: 500 }}>
                        {formatPlanName(cloudPlan)}
                      </span>
                    </span>
                  </div>
                )}
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
        <ScrollIndicator element={scrollEl} />
        {searchOpen && sessionId && items.length > 0 && (
          <ChatSearchBar
            client={client}
            sessionId={sessionId}
            historyIndexToItemIndex={historyIndexToItemIndex}
            liveItemCount={items.length}
            onJumpTo={onSearchJump}
            onClose={() => setSearchOpen(false)}
          />
        )}
        <div className="conversation-frame">
        {items.length === 0 ? (
          <div className="conversation">
            <div className="col">
              <div className="welcome">
                <div style={{ display: "flex", justifyContent: "center" }}>
                  <FlameIcon size={56} />
                </div>
                <h1>igni</h1>
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
            </div>
          </div>
        ) : (
          <Virtuoso
            ref={virtuosoRef}
            className="conversation"
            data={items}
            // Keys items by id so React reuses DOM across reorders /
            // edits. Falling back to index would defeat the memo on
            // ChatItemView.
            computeItemKey={(_idx, item) => item.id}
            // Auto-follow the tail only while the user is already
            // there; ``smooth`` is intentionally not used so streaming
            // doesn't visibly chase tokens.
            followOutput="auto"
            // Virtuoso's default ``atBottomThreshold`` is 4px which
            // misses the "at bottom" transition by a sub-pixel after
            // every layout change (composer height grows on Send,
            // streamed assistant content rewrites the last row, etc).
            // ``followOutput="auto"`` then bails on the follow because
            // its sample reads false. 50px is generous enough to
            // ride out the layout shifts without papering over a real
            // "I scrolled up to read history" intent — pinned by
            // ``e2e/chat-scroll.spec.ts``.
            atBottomThreshold={50}
            atBottomStateChange={setStickToBottom}
            scrollerRef={(el) => setScrollEl(el as HTMLElement | null)}
            increaseViewportBy={{ top: 400, bottom: 800 }}
            itemContent={(idx, item) => {
              // ``streaming`` lights the blinking caret CSS on the
              // live tail assistant row only.
              const isStreamingTail =
                processing && idx === items.length - 1 && item.kind === "assistant";
              const isSearchTarget = item.id === highlightedItemId;
              return (
                <div
                  className={`chat-row${isStreamingTail ? " streaming" : ""}${isSearchTarget ? " search-target" : ""}`}
                >
                  <ChatItemView
                    item={item}
                    copyResponseText={
                      item.kind === "stats"
                        ? findAssistantTextForStats(items, idx)
                        : undefined
                    }
                    onEditUser={onEditUser}
                    onDeleteUser={onDeleteUser}
                    onStopTeam={onStopTeam}
                    onStopAgent={onStopAgent}
                    onRetryAgent={onRetryAgent}
                    onApprovePlan={onApprovePlan}
                    onRejectPlan={onRejectPlan}
                    onDispatchVisualizationAction={onDispatchVisualizationAction}
                  />
                </div>
              );
            }}
            components={virtuosoComponents}
          />
        )}
        {items.length > 0 && !stickToBottom && (
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

        <Composer
          hitlSlot={
            hitl ? (
              <HitlDialog
                requirements={hitl}
                onResolve={(d) => void resolveHitl(d)}
                currentMode={status?.permission_mode}
                onAcceptEditsThisRun={() => {
                  // Mark so ``streaming_done`` reverts the flip.
                  autoAcceptForRunRef.current = true;
                  void runCommand("/accept on", false);
                }}
              />
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
          permissionMode={status?.permission_mode}
          onPickMode={(mode) => {
            // The split send button hands us one of the four
            // ``PermissionEvaluator.mode`` values. Each maps to
            // an existing slash command that flips the BE side.
            // ``default`` is "leave the current non-default
            // mode" — figure out which one is on and toggle it
            // off; if we were already in default the picker
            // wouldn't have fired (the menu hides the active
            // option as a no-op).
            //
            // We call ``client.handleCommand`` directly instead
            // of ``runCommand`` so the BE's confirmation chatter
            // ("Permission mode: plan → acceptEdits...") doesn't
            // land in the conversation. The user picked a mode
            // from the UI — they don't need a system message
            // narrating it. The ``permission_mode_changed`` push
            // broadcast still updates the badge / button tint
            // via the existing handler.
            const current = status?.permission_mode ?? "default";
            if (mode === current) return;
            const cmd =
              mode === "plan"
                ? "/plan on"
                : mode === "acceptEdits"
                  ? "/accept on"
                  : mode === "bypassPermissions"
                    ? "/bypass on"
                    : // mode === "default" — turn off whichever
                      // non-default mode is currently active.
                      current === "plan"
                      ? "/plan off"
                      : current === "acceptEdits"
                        ? "/accept off"
                        : current === "bypassPermissions"
                          ? "/bypass off"
                          : "";
            if (!cmd) return;
            void client.handleCommand(cmd).catch((e) => {
              // Surface real errors (network, BE shutdown) but
              // not the normal success-with-confirmation case.
              append(
                errorItem(
                  `Mode switch failed: ${e instanceof Error ? e.message : String(e)}`,
                ),
              );
            });
          }}
        />
        <div className="statusline" style={{ marginTop: 0, paddingBottom: 8 }}>
          {status && (
            <>
              <SessionChip sessionId={sessionId} />
              {/* Context meter sits next to the session chip —
                  both are "this session's identity / footprint"
                  signals and read more naturally side by side
                  than separated by the mode controls. */}
              <CtxMeter
                tokens={status.context_tokens}
                max={status.max_context}
                pct={ctxPct}
              />
              {/* Mode visibility moved to the send-button
                  picker itself — the left half tints per mode
                  so the user gets the at-a-glance signal
                  without a separate footer chip. */}
              <CodeIndexIndicator
                client={client}
                onOpen={() => setPanel({ kind: "codeindex" })}
              />
              <WatcherIndicator
                client={client}
                onOpen={() => setPanel({ kind: "watcher" })}
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
      {panel.kind === "watcher" && (
        <WatcherPanel client={client} onClose={() => setPanel({ kind: "none" })} />
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
