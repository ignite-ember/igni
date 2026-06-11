import { useEffect, useRef, useState } from "react";
import type { EmberClient } from "../protocol/client";

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
  { name: "/sessions", description: "List and switch sessions" },
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
];

interface MenuState {
  kind: "slash" | "mention";
  entries: { key: string; label: string; desc?: string }[];
  active: number;
  /** Index in the input text where the trigger token starts. */
  tokenStart: number;
}

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
  onTool,
  onSubmit,
  onStop,
}: {
  client: EmberClient;
  connected: boolean;
  processing: boolean;
  skills: SlashCommand[];
  tools: ToolEntry[];
  onTool: (command: string) => void;
  onSubmit: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [history, setHistory] = useState<string[]>([]);
  const [histIdx, setHistIdx] = useState(-1);
  const [draft, setDraft] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  const mentionSeq = useRef(0);

  const autoGrow = () => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  };

  const shellMode = text.startsWith("$");

  // ── Trigger detection: '/' at start, '@' anywhere ────────────────
  const refreshMenu = async (value: string, caret: number) => {
    // Slash menu: only when the input starts with '/' and the caret
    // is in the first token (mirrors TUI autocomplete behaviour).
    if (value.startsWith("/") && !value.slice(0, caret).includes(" ")) {
      const q = value.slice(1, caret).toLowerCase();
      const all = [...BUILTIN_COMMANDS, ...skills];
      const entries = all
        .filter((c) => c.name.slice(1).toLowerCase().startsWith(q))
        .slice(0, 12)
        .map((c) => ({ key: c.name, label: c.name, desc: c.description }));
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
          const files = await client.completeFiles(q, 12);
          if (seq !== mentionSeq.current) return; // stale response
          setMenu(
            files.length
              ? {
                  kind: "mention",
                  entries: files.map((f) => ({ key: f, label: f })),
                  active: 0,
                  tokenStart: at,
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
    const el = ref.current;
    if (!el || !menu) return;
    const caret = el.selectionStart ?? text.length;
    if (menu.kind === "slash") {
      setText(`${entry.key} `);
    } else {
      const before = text.slice(0, menu.tokenStart);
      const after = text.slice(caret);
      setText(`${before}@${entry.key} ${after}`);
    }
    setMenu(null);
    requestAnimationFrame(() => {
      el.focus();
      autoGrow();
    });
  };

  const submit = () => {
    const t = text.trim();
    if (!t) return;
    setHistory((h) => (h[h.length - 1] === t ? h : [...h, t].slice(-100)));
    setHistIdx(-1);
    setText("");
    setMenu(null);
    requestAnimationFrame(autoGrow);
    onSubmit(t);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
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
        applyMenuChoice(menu.entries[menu.active]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMenu(null);
        return;
      }
    }

    // Input history (only when caret is at the edge and no menu).
    if (e.key === "ArrowUp" && !text.includes("\n") && history.length) {
      e.preventDefault();
      const idx = histIdx === -1 ? history.length - 1 : Math.max(0, histIdx - 1);
      if (histIdx === -1) setDraft(text);
      setHistIdx(idx);
      setText(history[idx]);
      return;
    }
    if (e.key === "ArrowDown" && histIdx !== -1) {
      e.preventDefault();
      if (histIdx < history.length - 1) {
        setHistIdx(histIdx + 1);
        setText(history[histIdx + 1]);
      } else {
        setHistIdx(-1);
        setText(draft);
      }
      return;
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  useEffect(() => {
    autoGrow();
  }, [text]);

  return (
    <div className="composer-zone">
      <div className="composer">
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
        {menu && (
          <div className="popup-menu">
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
          </div>
        )}
        <textarea
          ref={ref}
          rows={1}
          value={text}
          disabled={!connected}
          placeholder={
            connected
              ? "Message Ember — / commands, @ files, $ shell"
              : "Connecting to backend…"
          }
          onChange={(e) => {
            setText(e.target.value);
            setHistIdx(-1);
            void refreshMenu(e.target.value, e.target.selectionStart ?? 0);
          }}
          onKeyDown={onKeyDown}
        />
        <div className="composer-bar">
          <button
            className={`slash-btn${toolsOpen ? " open" : ""}`}
            title="Tools & commands"
            onClick={() => setToolsOpen(!toolsOpen)}
          >
            /
          </button>
          {shellMode && <span className="mode-badge">$ shell</span>}
          <span className="composer-hint">
            Enter to send · Shift+Enter newline · ↑ history
          </span>
          <div className="header-spacer" />
          {processing ? (
            <button className="send-btn stop" title="Stop (Esc)" onClick={onStop}>
              ◼
            </button>
          ) : (
            <button
              className="send-btn"
              title="Send"
              disabled={!connected || !text.trim()}
              onClick={submit}
            >
              ↑
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
