import { useEffect, useRef, useState } from "react";
import type { EmberClient } from "../protocol/client";
import { host } from "../lib/host";
import { codePillLabels, EditableInput, type EditableInputHandle } from "./EditableInput";
import { ArrowUpIcon, ChevronIcon, StopIcon } from "./Icons";

export interface SlashCommand {
  name: string;
  description: string;
}

/** Built-in commands mirrored from the TUI's CommandHandler. Skills
 * are appended at runtime via get_skill_definitions. */
export const BUILTIN_COMMANDS: SlashCommand[] = [
  { name: "/help", description: "Show available commands" },
  { name: "/clear", description: "Start a new conversation" },
  { name: "/compact", description: "Summarize old context to free tokens" },
  { name: "/ctx", description: "Show context breakdown — floor vs conversation" },
  { name: "/sessions", description: "List and switch sessions" },
  { name: "/fork", description: "Fork this session — continue in a new id" },
  { name: "/model", description: "Pick a model" },
  { name: "/login", description: "Log in to Ember Cloud" },
  { name: "/logout", description: "Log out" },
  { name: "/mcp", description: "MCP servers — status and toggles" },
  { name: "/agents", description: "Agent pool" },
  { name: "/skills", description: "Skill workflows" },
  { name: "/plugins", description: "Installed plugins and marketplaces" },
  { name: "/knowledge", description: "Project knowledge base" },
  { name: "/codeindex", description: "Semantic code index" },
  { name: "/hooks", description: "Configured hooks" },
  { name: "/loop", description: "Repeat a prompt until done" },
  { name: "/schedule", description: "Background scheduled tasks" },
  { name: "/plan", description: "Toggle plan mode — agent proposes, you approve" },
  { name: "/accept", description: "Auto-approve file edits" },
  { name: "/bypass", description: "Skip permission prompts (scoped denies still apply)" },
  { name: "/memory", description: "View or edit project memory" },
  { name: "/rename", description: "Rename this session" },
  { name: "/config", description: "Show current settings" },
  { name: "/whoami", description: "Show the signed-in account" },
  { name: "/output-style", description: "Switch the agent's output style" },
  { name: "/plugin", description: "Install, update, remove plugins" },
  { name: "/sync-knowledge", description: "Push project knowledge to Ember Cloud" },
  { name: "/evals", description: "Run an evaluation suite" },
  { name: "/bug", description: "Open the bug report form" },
  { name: "/quit", description: "Exit the session" },
];

/** Prefix-filter the slash-command pool for the autocomplete menu.
 *  Mirrors what the composer's ``refreshMenu`` does for the slash
 *  branch — extracted as a pure helper so the filter contract
 *  (case-insensitive, prefix-only on the name after the leading
 *  '/', capped at 12 results) is testable without driving the
 *  contenteditable surface. Call with the full command pool
 *  (built-ins + skills) and the query text AFTER the ``/``. */
export function filterSlashCommands(
  pool: SlashCommand[],
  query: string,
  limit: number = 12,
): SlashCommand[] {
  const q = query.toLowerCase();
  return pool
    .filter((c) => c.name.slice(1).toLowerCase().startsWith(q))
    .slice(0, limit);
}

interface MenuState {
  kind: "slash" | "mention";
  entries: { key: string; label: string; desc?: string }[];
  active: number;
  /** Index in the input text where the trigger token starts. */
  tokenStart: number;
  /** For mention menus: total matches before truncation. The popup
   *  paginates on scroll, so this is how we know whether there's
   *  more to load. */
  total?: number;
  /** Mention menus: current limit asked from the BE. Bumped on
   *  scroll to bottom; reset to PAGE_SIZE on each new query. */
  limit?: number;
  /** Mention menus: the query at the time of fetch — needed because
   *  scroll-to-bottom triggers a new fetch and we must reuse it. */
  query?: string;
}

const MENTION_PAGE_SIZE = 200;

/**
 * Claude-style composer: auto-growing textarea, `/` command menu,
 * `@` file mentions (BE-side FileIndex), `$` shell mode badge,
 * Up/Down input history, send/stop button.
 */
export interface ToolEntry {
  label: string;
  command: string;
  desc: string;
}

export function Composer({
  client,
  connected,
  processing,
  skills,
  tools,
  seed,
  sessionId,
  clientState,
  model,
  modelMenuSignal,
  hitlSlot,
  onPickModel,
  onTool,
  onTyping,
  onSubmit,
  onStop,
  permissionMode,
  onPickMode,
}: {
  client: EmberClient;
  connected: boolean;
  processing: boolean;
  skills: SlashCommand[];
  tools: ToolEntry[];
  /** Pre-fill request (e.g. a skill picked from the panel) — bump
   *  `n` to re-apply the same text. */
  seed?: { text: string; n: number } | null;
  /** Active session id — drafts are persisted per-session so
   *  switching sessions doesn't trash the in-progress message. */
  sessionId?: string;
  /** Per-client state store — used to persist composer drafts on
   *  the BE so they survive reload from any host (web / IDE plugin). */
  clientState?: import("../clientState").ClientStateStore;
  /** Current model name, shown as a picker chip next to Send. */
  model?: string;
  /** External request to open the model menu (the /model command). */
  modelMenuSignal?: { n: number } | null;
  /** Slot rendered inside the composer box, above the editor.
   *  Used for the HITL approval form so it visually rises out of
   *  the input panel instead of floating above it. */
  hitlSlot?: React.ReactNode;
  onPickModel: (name: string) => void;
  onTool: (command: string) => void;
  onTyping?: (text: string) => void;
  onSubmit: (text: string) => void;
  onStop: () => void;
  /** Current ``PermissionEvaluator.mode`` value from the BE
   *  (``default`` / ``plan`` / ``acceptEdits`` / ``bypassPermissions``).
   *  Used to pre-select the active mode in the send-button
   *  dropdown. */
  permissionMode?: string;
  /** Called when the user picks a different mode from the
   *  send-button dropdown. The host (App.tsx) is responsible
   *  for firing the matching slash command so the BE flips
   *  ``permission_mode`` accordingly; the dropdown is purely a
   *  trigger surface. */
  onPickMode?: (mode: string) => void;
}) {
  const draftKey = sessionId ? `draft:${sessionId}` : "";
  const [text, setText] = useState("");
  // Hydrate the draft whenever the session changes. clientState is
  // already populated by the time the App sets a sessionId, so the
  // cache hit is synchronous.
  useEffect(() => {
    if (!draftKey || !clientState) {
      setText("");
      return;
    }
    setText(clientState.get(draftKey) || "");
  }, [draftKey, clientState]);
  // Mirror text → BE on change (debounced inside clientState). When
  // the draft empties out, drop the row so the store stays tidy.
  useEffect(() => {
    if (!draftKey || !clientState) return;
    if (text) clientState.set(draftKey, text);
    else if (clientState.get(draftKey)) clientState.delete(draftKey);
  }, [text, draftKey, clientState]);
  /** Files uploaded from the OS (picker / drag / paste). Each one
   *  is shipped to the BE, which writes it to
   *  ``<project>/.ember/attachments/<session>/<name>`` and returns
   *  the path; on submit we prepend ``@<path>`` so the existing
   *  @-mention pipeline resolves it (no inline content). */
  const [attachments, setAttachments] = useState<
    { path: string; name: string; uploading?: boolean }[]
  >([]);
  /** Inline code pills inserted by the paste handler. Each entry
   *  maps a short id → its snippet text and ref list. The id appears
   *  in the canonical editor text as ``@code:<id>``; on submit we
   *  expand each token back to a fenced code block plus its refs.
   *  Using a ref (not state) because mutations don't drive any
   *  render path other than ``codePillLabels`` (also a Map). */
  const codePillData = useRef<
    Map<
      string,
      {
        snippet: string;
        refs: {
          path: string;
          line: number;
          end_line?: number;
          preview: string;
        }[];
      }
    >
  >(new Map());
  const codePillIdCounter = useRef(0);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragHot, setDragHot] = useState(false);

  const pickFiles = () => fileInputRef.current?.click();

  const uploadOne = async (file: File): Promise<{ path: string; name: string } | null> => {
    if (file.size > 5 * 1024 * 1024) {
      // 5MB ceiling — base64 over WS is wasteful for huge files;
      // larger payloads should land via the file system directly.
      console.warn("Attachment too large:", file.name, file.size);
      return null;
    }
    const buf = await file.arrayBuffer();
    // btoa needs binary string; chunk so we don't blow the call
    // stack on bigger blobs.
    let bin = "";
    const view = new Uint8Array(buf);
    const CHUNK = 0x8000;
    for (let i = 0; i < view.length; i += CHUNK) {
      bin += String.fromCharCode.apply(null, Array.from(view.subarray(i, i + CHUNK)));
    }
    const content_base64 = btoa(bin);
    const res = await client.rpc<{ path: string; size: number; error?: string }>(
      "upload_attachment",
      { filename: file.name, content_base64 },
    );
    if (!res.path) return null;
    return { path: res.path, name: file.name };
  };

  const addFiles = async (files: File[] | FileList | null) => {
    if (!files) return;
    const list = Array.from(files);
    if (!list.length) return;
    // Optimistic placeholder rows so the chips appear instantly.
    const placeholders = list.map((f) => ({ path: `pending:${f.name}-${Date.now()}-${Math.random()}`, name: f.name, uploading: true }));
    setAttachments((prev) => [...prev, ...placeholders]);
    for (let i = 0; i < list.length; i++) {
      const file = list[i];
      const placeholder = placeholders[i];
      try {
        const result = await uploadOne(file);
        // Drop the placeholder either way; on success, inject the
        // path into the editor as an `@<path>` reference. The
        // EditableInput renders it as a pill automatically.
        setAttachments((prev) => prev.filter((a) => a.path !== placeholder.path));
        if (result) {
          setText((prev) => {
            const sep = prev && !prev.endsWith(" ") && !prev.endsWith("\n") ? " " : "";
            return `${prev}${sep}@${result.path} `;
          });
          requestAnimationFrame(() => ref.current?.caretToEnd());
        }
      } catch (e) {
        console.error("upload failed", e);
        setAttachments((prev) => prev.filter((a) => a.path !== placeholder.path));
      }
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  // TUI parity: typing "/" or "$" consumes the prefix into a mode
  // badge — the input shows only the command body. Backspace on an
  // empty input exits the mode.
  const [mode, setMode] = useState<"chat" | "command" | "shell">("chat");
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [modelMenu, setModelMenu] = useState<{ name: string; current: boolean }[] | null>(null);
  // Mode-picker dropdown for the split send button. ``true`` =
  // open. Mode selection itself happens via ``onPickMode``;
  // the popup just surfaces the options.
  const [modeMenu, setModeMenu] = useState(false);
  const [history, setHistory] = useState<string[]>([]);
  const [histIdx, setHistIdx] = useState(-1);
  const [draft, setDraft] = useState("");
  const ref = useRef<EditableInputHandle>(null);
  // Mirror of `text` for stale-closure-safe reads inside synchronous
  // key handlers. React state updates lag a render behind, so a
  // burst of Backspace presses kept seeing the pre-emptied text.
  const textRef = useRef("");
  useEffect(() => {
    textRef.current = text;
  }, [text]);
  const mentionSeq = useRef(0);
  // Re-entrancy guard so a rapid scroll doesn't fire dozens of
  // overlapping completeFiles RPCs.
  const loadingMore = useRef(false);

  const loadMoreMentions = async () => {
    if (loadingMore.current) return;
    if (!menu || menu.kind !== "mention") return;
    if (typeof menu.total !== "number" || typeof menu.limit !== "number") return;
    if (menu.entries.length >= menu.total) return;
    loadingMore.current = true;
    const seq = ++mentionSeq.current;
    const nextLimit = menu.limit + MENTION_PAGE_SIZE;
    try {
      const { matches, total } = await client.completeFiles(
        menu.query || "", nextLimit,
      );
      if (seq !== mentionSeq.current) return;
      setMenu((cur) =>
        cur && cur.kind === "mention" && cur.query === menu.query
          ? {
              ...cur,
              entries: matches.map((f) => ({ key: f, label: f })),
              total,
              limit: nextLimit,
            }
          : cur,
      );
    } catch {
      /* keep current entries */
    } finally {
      loadingMore.current = false;
    }
  };

  /** Auto-grow no-op: the contenteditable wraps naturally; keep the
   *  function so existing call sites compile without behaviour
   *  change. The max-height is enforced by CSS. */
  const autoGrow = () => {};

  const shellMode = mode === "shell";
  const commandMode = mode === "command";

  /** The wire-form text: mode prefix + input body. */
  const withPrefix = (value: string, m = mode) =>
    m === "command" ? `/${value}` : m === "shell" ? `$ ${value}` : value;

  /** Set input from a full (prefixed) string, entering the right mode. */
  const setFromFull = (entry: string) => {
    if (entry.startsWith("/")) {
      setMode("command");
      setText(entry.slice(1));
    } else if (entry.startsWith("$")) {
      setMode("shell");
      setText(entry.slice(1).trimStart());
    } else {
      setMode("chat");
      setText(entry);
    }
  };

  // ── Trigger detection: '/' at start, '@' anywhere ────────────────
  const refreshMenu = async (value: string, caret: number) => {
    // Slash menu: only when the input starts with '/' and the caret
    // is in the first token (mirrors TUI autocomplete behaviour).
    if (value.startsWith("/") && !value.slice(0, caret).includes(" ")) {
      const q = value.slice(1, caret);
      const entries = filterSlashCommands([...BUILTIN_COMMANDS, ...skills], q).map(
        (c) => ({ key: c.name, label: c.name, desc: c.description }),
      );
      setMenu(
        entries.length ? { kind: "slash", entries, active: 0, tokenStart: 0 } : null,
      );
      return;
    }

    // Mention menu: '@' token under the caret.
    const upToCaret = value.slice(0, caret);
    const at = upToCaret.lastIndexOf("@");
    if (at >= 0 && (at === 0 || /\s/.test(upToCaret[at - 1]))) {
      const q = upToCaret.slice(at + 1);
      if (!/\s/.test(q)) {
        const seq = ++mentionSeq.current;
        try {
          // Start with a small page; the popup paginates on scroll
          // so we don't ship a 50k-file blob on every keystroke.
          const { matches, total } = await client.completeFiles(
            q, MENTION_PAGE_SIZE,
          );
          if (seq !== mentionSeq.current) return; // stale response
          setMenu(
            matches.length
              ? {
                  kind: "mention",
                  entries: matches.map((f) => ({ key: f, label: f })),
                  active: 0,
                  tokenStart: at,
                  total,
                  limit: MENTION_PAGE_SIZE,
                  query: q,
                }
              : null,
          );
        } catch {
          setMenu(null);
        }
        return;
      }
    }
    setMenu(null);
  };

  const applyMenuChoice = (entry: { key: string }) => {
    if (!menu) return;
    const caret = ref.current?.caret() ?? text.length;
    if (menu.kind === "slash") {
      setText(commandMode ? `${entry.key.slice(1)} ` : `${entry.key} `);
    } else {
      const before = text.slice(0, menu.tokenStart);
      const after = text.slice(caret);
      setText(`${before}@${entry.key} ${after}`);
    }
    setMenu(null);
    requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.caretToEnd();
    });
  };

  const submit = () => {
    const raw = text.trim();
    if (attachments.some((a) => a.uploading)) return; // wait for uploads
    if (!raw) return;
    // The editor body already contains `@<path>` tokens for every
    // reference — uploads inject them on completion, and inline
    // typing renders pills directly. Just send what's there.
    let t = withPrefix(raw);
    // Expand inline ``@code:<id>`` pills into a structured block the
    // user bubble will render as the SAME inline pill the composer
    // showed. Format:
    //
    //   [code-paste hello.py:9 lines=9-16]
    //   <snippet>
    //   [/code-paste]
    //
    // ``path:firstLine`` anchors the click-to-open action; ``lines=…``
    // carries the dedup-and-range-collapsed line spec the pill label
    // displays. The snippet content sits between the tags so the
    // model still sees it as part of the message body.
    t = t.replace(/(?:^|(?<=\s))@code:(\S+)(?=\s|$)/g, (_match, id: string) => {
      const data = codePillData.current.get(id);
      if (!data) return ""; // unknown id → drop the marker silently
      // Same range-merging logic as the paste-time label builder:
      // group matches by path, fold (start, end_line) tuples into
      // compact ranges so a multi-line paste round-trips as
      // ``lines=71-75`` (and the user-bubble pill clicks open that
      // exact range in the IDE). ``end_line`` comes from the BE; an
      // older BE that doesn't send it falls back to a single-line
      // range, which is still correct, just less informative.
      const byPath = new Map<string, [number, number][]>();
      for (const r of data.refs) {
        const start = r.line;
        const end = Math.max(start, (r as { end_line?: number }).end_line ?? start);
        if (!byPath.has(r.path)) byPath.set(r.path, []);
        byPath.get(r.path)!.push([start, end]);
      }
      // Anchor on the first file (matches the composer label rule).
      const [firstPath, firstRanges] = byPath.entries().next().value!;
      const sorted = firstRanges.slice().sort((a, b) => a[0] - b[0]);
      const merged: [number, number][] = [];
      for (const [s, e] of sorted) {
        const last = merged[merged.length - 1];
        if (last && s <= last[1] + 1) {
          last[1] = Math.max(last[1], e);
        } else {
          merged.push([s, e]);
        }
      }
      const lineSpec = merged
        .map(([s, e]) => (s === e ? String(s) : `${s}-${e}`))
        .join(",");
      const anchor = merged[0][0];
      return `[code-paste ${firstPath}:${anchor} lines=${lineSpec}]\n${data.snippet}\n[/code-paste]`;
    });
    setHistory((h) => (h[h.length - 1] === t ? h : [...h, t].slice(-100)));
    setHistIdx(-1);
    setText("");
    setMode("chat");
    setMenu(null);
    onTyping?.(""); // clear our remote draft on other views
    requestAnimationFrame(autoGrow);
    setAttachments([]);
    // Don't blanket-clear the code-pill data — the editor's @code:<id>
    // tokens have just been consumed above, and a fresh paste will
    // generate a new id. Old ids stay so any history entry the user
    // recalls (ArrowUp) still has its pills resolvable.
    onSubmit(t);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (menu) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const delta = e.key === "ArrowDown" ? 1 : -1;
        setMenu({
          ...menu,
          active: (menu.active + delta + menu.entries.length) % menu.entries.length,
        });
        return;
      }
      if (e.key === "Tab" || e.key === "Enter") {
        e.preventDefault();
        const entry = menu.entries[menu.active];
        // Enter on an already-complete command runs it — otherwise a
        // fully-typed "/help" would need Enter twice (complete, send).
        if (e.key === "Enter" && menu.kind === "slash" && entry.key === withPrefix(text.trim())) {
          setMenu(null);
          submit();
          return;
        }
        applyMenuChoice(entry);
        return;
      }
    }

    // Escape: universal "abandon what I'm typing" key. Handled
    // OUTSIDE the ``if (menu)`` block above so a command-mode
    // input WITHOUT an open menu (e.g. user typed ``/p`` then
    // arrowed down then arrowed back up past the menu somehow,
    // or the menu auto-closed because the query stopped matching)
    // still has a way out without backspacing every character.
    //
    // Order of clearing:
    //   1. If the menu is open, close it.
    //   2. If we're in command/shell mode (regardless of text
    //      content), exit to chat mode + clear the field.
    //   3. If neither applies, let the event propagate so the
    //      app-level Esc (cancel run) handler can fire.
    if (e.key === "Escape") {
      let consumed = false;
      if (menu) {
        setMenu(null);
        consumed = true;
      }
      if (mode !== "chat") {
        setMode("chat");
        setText("");
        ref.current?.setValue("");
        onTyping?.("");
        consumed = true;
      }
      if (consumed) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      // Falls through to the app-level Esc (cancel run).
    }

    // Backspace exits command/shell mode when there's nothing
    // useful left to delete. Two thresholds:
    //   - text already empty → exit (existing behavior).
    //   - text is 1 char → backspace would empty it; exit on
    //     the SAME press instead of leaving an empty command-
    //     mode prompt that needs a second backspace to escape.
    // Backspace with 2+ chars deletes one character normally so
    // ``/list`` → ``/lis`` works for fixing typos.
    if (e.key === "Backspace" && mode !== "chat" && textRef.current.length <= 1) {
      e.preventDefault();
      setMode("chat");
      setText("");
      ref.current?.setValue("");
      setMenu(null);
      onTyping?.("");
      return;
    }

    // Code pills now live inline in the editor — EditableInput's
    // existing pill-delete-as-unit Backspace logic handles them.

    // Input history (only when caret is at the edge and no menu).
    // Entries are stored in wire form ("/help", "$ ls") — recalling
    // one re-enters the matching mode with the prefix consumed.
    // History-recall ArrowUp only fires when the editor caret is
    // already at the top of the input (single-line case).
    if (
      e.key === "ArrowUp" &&
      !text.includes("\n") &&
      history.length
    ) {
      e.preventDefault();
      const idx = histIdx === -1 ? history.length - 1 : Math.max(0, histIdx - 1);
      if (histIdx === -1) setDraft(withPrefix(text));
      setHistIdx(idx);
      setFromFull(history[idx]);
      return;
    }
    if (e.key === "ArrowDown" && histIdx !== -1) {
      e.preventDefault();
      if (histIdx < history.length - 1) {
        setHistIdx(histIdx + 1);
        setFromFull(history[histIdx + 1]);
      } else {
        setHistIdx(-1);
        setFromFull(draft);
      }
      return;
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const openModelMenu = async () => {
    try {
      const reg = await client.rpc<{ registry: Record<string, unknown>; default: string }>(
        "get_model_registry",
      );
      setModelMenu(
        Object.keys(reg.registry)
          .sort()
          .map((name) => ({ name, current: name === reg.default })),
      );
    } catch {
      /* registry unavailable */
    }
  };

  // The /model slash command routes here so there's a single picker UI.
  useEffect(() => {
    if (modelMenuSignal) void openModelMenu();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelMenuSignal?.n]);

  // Esc closes the model picker (and must not cancel a running turn).
  useEffect(() => {
    if (!modelMenu) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setModelMenu(null);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [modelMenu]);

  // External pre-fill (skill picked from the panel): set the text and
  // hand the user the caret so they can append arguments and submit.
  useEffect(() => {
    if (!seed) return;
    setFromFull(seed.text);
    setMenu(null);
    setHistIdx(-1);
    requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.caretToEnd();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed?.n]);

  return (
    <div
      className={`composer-zone${dragHot ? " drag-hot" : ""}`}
      onDragOver={(e) => {
        if (e.dataTransfer?.types?.includes("Files")) {
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
          if (!dragHot) setDragHot(true);
        }
      }}
      onDragLeave={(e) => {
        // Only clear when leaving the wrapper itself, not its children.
        if (e.target === e.currentTarget) setDragHot(false);
      }}
      onDrop={(e) => {
        if (e.dataTransfer?.files?.length) {
          e.preventDefault();
          setDragHot(false);
          void addFiles(e.dataTransfer.files);
        }
      }}
    >
      <div className="composer">
        {hitlSlot && <div className="composer-hitl">{hitlSlot}</div>}
        {toolsOpen && (
          <>
            <div
              style={{ position: "fixed", inset: 0, zIndex: 29 }}
              onClick={() => setToolsOpen(false)}
            />
            <div className="popup-menu">
              {tools.map((t) => (
                <div
                  key={t.command}
                  className="popup-item"
                  onClick={() => {
                    setToolsOpen(false);
                    onTool(t.command);
                  }}
                >
                  <span className="cmd">{t.label}</span>
                  <span className="desc">{t.desc}</span>
                </div>
              ))}
            </div>
          </>
        )}
        {modelMenu && (
          <>
            <div
              style={{ position: "fixed", inset: 0, zIndex: 29 }}
              onClick={() => setModelMenu(null)}
            />
            <div className="popup-menu model-menu">
              {modelMenu.map((m) => (
                <div
                  key={m.name}
                  className={`popup-item ${m.current ? "active" : ""}`}
                  onClick={() => {
                    setModelMenu(null);
                    onPickModel(m.name);
                  }}
                >
                  <span className="cmd">{m.name}</span>
                  {m.current && <span className="desc">current</span>}
                </div>
              ))}
            </div>
          </>
        )}
        {menu && (
          <div
            className="popup-menu"
            onScroll={(e) => {
              if (menu.kind !== "mention") return;
              const el = e.currentTarget;
              // Fire when we're within ~120px of the bottom — gives
              // the fetch a head start before the user runs out of rows.
              if (el.scrollHeight - el.scrollTop - el.clientHeight < 120) {
                void loadMoreMentions();
              }
            }}
          >
            {menu.entries.map((entry, i) => (
              <div
                key={entry.key}
                className={`popup-item ${i === menu.active ? "active" : ""}`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  applyMenuChoice(entry);
                }}
              >
                <span className="cmd">{entry.label}</span>
                {entry.desc && <span className="desc">{entry.desc}</span>}
              </div>
            ))}
            {menu.kind === "mention" &&
              typeof menu.total === "number" &&
              menu.total > menu.entries.length && (
                <div className="popup-footer" aria-live="polite">
                  Showing {menu.entries.length.toLocaleString()} of{" "}
                  {menu.total.toLocaleString()} — scroll to load more, or type
                  to narrow
                </div>
              )}
          </div>
        )}
        {/* Uploads in flight still need a strip so the user sees
            progress before they're written to disk. Once the BE
            returns a path the user picks them up as live pills
            inside the editor (we auto-insert `@<path>` for them). */}
        {attachments.some((a) => a.uploading) && (
          <div className="composer-attachments">
            {attachments
              .filter((a) => a.uploading)
              .map((a) => (
                <span
                  key={a.path}
                  className="composer-chip-attach uploading"
                  title={`${a.name} (uploading…)`}
                >
                  <span className="composer-chip-name">{a.name}</span>
                </span>
              ))}
          </div>
        )}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={(e) => void addFiles(e.target.files)}
        />
        <EditableInput
          ref={ref}
          value={text}
          disabled={!connected}
          className={commandMode ? "mode-command" : shellMode ? "mode-shell" : ""}
          placeholder={
            !connected
              ? "Connecting to backend…"
              : commandMode
                ? "Command name (Backspace to return to chat)"
                : shellMode
                  ? "Shell command (Backspace to return to chat)"
                  : "Message Ember — / commands, @ files, $ shell"
          }
          onValueChange={(value, caret) => {
            setHistIdx(-1);
            if (mode === "chat" && (value.startsWith("/") || value.startsWith("$"))) {
              const m = value.startsWith("/") ? "command" : "shell";
              const body = value.slice(1).replace(/^ /, "");
              setMode(m);
              setText(body);
              // React would see no `text` change here (often "" → ""
              // when the user just typed the trigger char), so the
              // editor's value-prop reconcile won't fire. Force the
              // DOM to match imperatively so the literal "/" or "$"
              // doesn't linger in the editor.
              ref.current?.setValue(body);
              onTyping?.(withPrefix(body, m));
              void refreshMenu(withPrefix(body, m), Math.max(caret, 1));
              return;
            }
            setText(value);
            onTyping?.(withPrefix(value));
            void refreshMenu(
              mode === "command" ? `/${value}` : value,
              mode === "command" ? caret + 1 : caret,
            );
          }}
          onKeyDown={onKeyDown}
          onPaste={(e) => {
            const files = Array.from(e.clipboardData?.files || []);
            if (files.length) {
              e.preventDefault();
              void addFiles(files);
              return;
            }
            // Code-paste detection: multi-line, sizeable text → ask
            // the BE if it lives anywhere in the repo. The paste itself
            // is NOT intercepted — the code goes into the editor as
            // usual. We just decorate the composer with a pill row of
            // "found in X, Y" so the user can see (and click into) the
            // repo locations the snippet matches.
            const pasted = e.clipboardData?.getData("text") || "";
            if (pasted.length >= 30 && pasted.includes("\n")) {
              // Take over the paste. contenteditable mangles multi-line
              // text — it splits paragraphs across <div>/<br> nodes,
              // sometimes adds an extra \n at section boundaries — and
              // that mangling means our later ``text.indexOf(pasted)``
              // would miss for snippets ≳ a few lines. By computing the
              // new text ourselves and feeding it through setText, the
              // editor's text exactly matches what we searched for.
              e.preventDefault();
              const caret = ref.current?.caret() ?? text.length;
              setText((cur) => cur.slice(0, caret) + pasted + cur.slice(caret));
              requestAnimationFrame(() =>
                ref.current?.setCaretAt(caret + pasted.length),
              );
              void (async () => {
                try {
                  // Prefer the host's indexed search (JetBrains uses
                  // PyCharm's trigram index — ~100ms vs ~5s for the
                  // ``rg`` subprocess RPC on big projects). If the
                  // host returns null we fall through to the BE RPC,
                  // which still handles every other client (web,
                  // Tauri, VSCode) plus JetBrains while the project
                  // is still indexing.
                  const res =
                    (await host.searchCode(pasted)) ??
                    (await client.rpc<{
                      matches: {
                        path: string;
                        line: number;
                        end_line?: number;
                        preview: string;
                      }[];
                    }>("search_code", { snippet: pasted }));
                  if (res.matches?.length) {
                    // Build a friendly label from the first match's
                    // path + line span. ``end_line`` covers the case
                    // where the pasted snippet spans multiple lines —
                    // rg's --multiline only reports the start row, so
                    // the BE attaches the end derived from the snippet
                    // itself. A 5-line paste reads as "71-75" instead
                    // of just "71". Falls back to ``line`` for single-
                    // line matches and for older BEs.
                    const grouped = new Map<string, [number, number][]>();
                    for (const r of res.matches) {
                      const start = r.line;
                      const end = Math.max(start, r.end_line ?? start);
                      if (!grouped.has(r.path)) grouped.set(r.path, []);
                      grouped.get(r.path)!.push([start, end]);
                    }
                    const [firstPath, firstRanges] = grouped
                      .entries()
                      .next().value!;
                    // Merge overlapping or adjacent ranges so non-
                    // contiguous matches still render compactly.
                    const sorted = firstRanges
                      .slice()
                      .sort((a, b) => a[0] - b[0]);
                    const merged: [number, number][] = [];
                    for (const [s, e] of sorted) {
                      const last = merged[merged.length - 1];
                      if (last && s <= last[1] + 1) {
                        last[1] = Math.max(last[1], e);
                      } else {
                        merged.push([s, e]);
                      }
                    }
                    const ranges = merged.map(([s, e]) =>
                      s === e ? String(s) : `${s}-${e}`,
                    );
                    const filename = firstPath.split("/").pop() || firstPath;
                    const extraFiles = grouped.size - 1;
                    const label =
                      `${filename} ${ranges.join(", ")}` +
                      (extraFiles > 0 ? ` +${extraFiles}` : "");

                    codePillIdCounter.current += 1;
                    const id = `c${codePillIdCounter.current}`;
                    codePillData.current.set(id, { snippet: pasted, refs: res.matches });
                    codePillLabels.set(id, label);

                    // Swap the just-pasted snippet for an ``@code:<id>``
                    // token. The editor renders it as an inline pill;
                    // submit() walks the canonical text and expands
                    // each token back to a fenced block + refs. We
                    // pad with spaces so the pill regex
                    // ``(?:^|(?<=\s))@(\S+)(?=\s|$)`` sees the token as
                    // a standalone word.
                    let caretAfter = 0;
                    setText((cur) => {
                      const at = cur.indexOf(pasted);
                      if (at < 0) return cur;
                      const before = cur.slice(0, at);
                      const after = cur.slice(at + pasted.length);
                      const lead = before.endsWith(" ") || before === "" || before.endsWith("\n") ? "" : " ";
                      const trail = after.startsWith(" ") || after === "" || after.startsWith("\n") ? " " : "";
                      const token = `${lead}@code:${id}${trail}`;
                      caretAfter = before.length + token.length;
                      return `${before}${token}${after}`;
                    });
                    // Position the caret AFTER the pill (+ its trailing
                    // space if any) so the next keystroke lands past it.
                    // Browsers default the post-paste caret to the end
                    // of the pasted text, which lands before our shorter
                    // token, so we explicitly re-anchor.
                    requestAnimationFrame(() => ref.current?.setCaretAt(caretAfter));
                  }
                } catch (err) {
                  console.warn("search_code failed", err);
                }
              })();
            }
          }}
        />
        <div className="composer-bar">
          <button
            className={`slash-btn${toolsOpen || commandMode ? " open" : ""}`}
            title="Tools & commands"
            onClick={() => setToolsOpen(!toolsOpen)}
          >
            /
          </button>
          <button
            className="slash-btn attach-btn"
            title="Attach files"
            onClick={pickFiles}
          >
            +
          </button>
          {shellMode && <span className="mode-badge">$ shell</span>}
          {commandMode && <span className="mode-badge command">/ command</span>}
          <span className="composer-hint">
            Enter to send · Shift+Enter newline · ↑ history
          </span>
          <div className="header-spacer" />
          {model && (
            <button
              className="chip composer-model"
              title="Switch model"
              onClick={() => (modelMenu ? setModelMenu(null) : void openModelMenu())}
            >
              {model} <ChevronIcon size={9} down />
            </button>
          )}
          {processing ? (
            <button className="send-btn stop" title="Stop (Esc)" onClick={onStop}>
              <StopIcon />
            </button>
          ) : (
            <SendButton
              connected={connected}
              canSend={!!text.trim() || attachments.length > 0}
              onSubmit={submit}
              permissionMode={permissionMode ?? "default"}
              onPickMode={onPickMode}
              modeMenuOpen={modeMenu}
              setModeMenuOpen={setModeMenu}
            />
          )}
        </div>
      </div>
    </div>
  );
}

/** Split send button — left half is a dropdown trigger that
 *  reveals the four permission modes; right half is the actual
 *  send action. The user can change the agent's permission
 *  surface at the moment of sending instead of digging into
 *  ``/plan`` / ``/accept`` / ``/bypass`` slash commands. The
 *  left half is intentionally narrower than the right (~3:5
 *  ratio) so the primary action — Send — stays the visually
 *  dominant target.
 */
const MODE_OPTIONS: { value: string; label: string; desc: string }[] = [
  {
    value: "default",
    label: "Execute",
    desc: "Ask before each mutating tool. The safe default.",
  },
  {
    value: "plan",
    label: "Plan",
    desc: "Read-only sandbox. Agent drafts a plan card; you approve before any edits run.",
  },
  {
    value: "acceptEdits",
    label: "Auto-edit",
    desc: "Auto-approve file edits (Edit / Write / NotebookEdit). Shell + web tools still ask.",
  },
  {
    value: "bypassPermissions",
    label: "Bypass",
    desc: "Auto-approve every tool, including shell. Explicit deny rules in settings still block.",
  },
];

function SendButton({
  connected,
  canSend,
  onSubmit,
  permissionMode,
  onPickMode,
  modeMenuOpen,
  setModeMenuOpen,
}: {
  connected: boolean;
  canSend: boolean;
  onSubmit: () => void;
  permissionMode: string;
  onPickMode?: (mode: string) => void;
  modeMenuOpen: boolean;
  setModeMenuOpen: (next: boolean) => void;
}) {
  const current =
    MODE_OPTIONS.find((m) => m.value === permissionMode) ?? MODE_OPTIONS[0];
  return (
    <div className="send-split" data-mode={current.value}>
      <button
        type="button"
        className="send-split-mode"
        title={`Mode: ${current.desc}`}
        onClick={() => setModeMenuOpen(!modeMenuOpen)}
        aria-haspopup="menu"
        aria-expanded={modeMenuOpen}
      >
        <span className="send-split-mode-label">{current.label}</span>
        <ChevronIcon size={9} down={!modeMenuOpen} />
      </button>
      <button
        type="button"
        className="send-split-go"
        title="Send"
        disabled={!connected || !canSend}
        onClick={onSubmit}
      >
        <ArrowUpIcon />
      </button>
      {modeMenuOpen && (
        <>
          {/* Backdrop closes the menu on outside click — same
              pattern the model picker uses. */}
          <div
            className="popup-backdrop"
            onClick={() => setModeMenuOpen(false)}
          />
          <div className="popup-menu send-mode-menu" role="menu">
            {MODE_OPTIONS.map((m) => (
              <div
                key={m.value}
                role="menuitem"
                className={`popup-item ${m.value === current.value ? "active" : ""}`}
                onClick={() => {
                  setModeMenuOpen(false);
                  if (m.value !== current.value) onPickMode?.(m.value);
                }}
              >
                <span className="cmd">{m.label}</span>
                <span className="desc">{m.desc}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

