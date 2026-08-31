import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { EmberClient } from "../../protocol/client";
import { Drawer } from "./Drawer";
import { ChevronIcon } from "../Icons";
import { Skeleton } from "../Skeleton";

interface McpResource {
  uri: string;
  name: string;
  description: string;
  mime_type: string;
}

interface McpPrompt {
  name: string;
  description: string;
  arguments: string[];
}

interface McpServer {
  name: string;
  connected: boolean;
  transport: string;
  toolNames: string[];
  toolDescriptions: Record<string, string>;
  toolsDisabled: Set<string>;
  resources: McpResource[];
  prompts: McpPrompt[];
  error?: string;
  policyBlocked: boolean;
}

/** Small wrench glyph — same outline weight as other panel icons. */
function ToolGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M10.5 2a3.5 3.5 0 0 0-3.4 4.3L2.4 11l1 1 1.6-1.6 1 1L4.5 13l1 1L10.2 9.3a3.5 3.5 0 0 0 4.3-4.4l-2 2-1.7-.4-.4-1.7 2-2z"
        stroke="currentColor"
        strokeWidth="1.1"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function FileGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M3.5 1.5h5L12.5 5v9a1 1 0 0 1-1 1h-8a1 1 0 0 1-1-1V2.5a1 1 0 0 1 1-1z"
        stroke="currentColor"
        strokeWidth="1.1"
        strokeLinejoin="round"
      />
      <path d="M8.5 1.5V5h4" stroke="currentColor" strokeWidth="1.1" />
    </svg>
  );
}

function SparkGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M8 2v3M8 11v3M2 8h3M11 8h3M4.2 4.2l2 2M9.8 9.8l2 2M4.2 11.8l2-2M9.8 6.2l2-2"
        stroke="currentColor"
        strokeWidth="1.1"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** Two-line clamp for tool descriptions with a Show more / less toggle.
 *  Falls back to flat text when the content fits — we only render the
 *  toggle if the content was actually truncated. */
function ClampedText({ children }: { children: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    // Measure against the clamped state — if the natural height
    // exceeds the visible height we need the toggle.
    setOverflowing(el.scrollHeight - el.clientHeight > 1);
  }, [children]);

  return (
    <div className="mcp-tool-desc-wrap">
      <div
        ref={ref}
        className={`mcp-tool-desc ${expanded ? "expanded" : "clamped"}`}
      >
        {children}
      </div>
      {(overflowing || expanded) && (
        <button
          type="button"
          className="mcp-tool-more"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "Show less" : "Show more…"}
        </button>
      )}
    </div>
  );
}

/** Strip the server prefix Agno adds (e.g. ``filesystem__read_file``
 *  → ``read_file``) so the row reads cleanly. */
function shortToolName(full: string): { short: string; prefix?: string } {
  const m = full.match(/^([a-zA-Z0-9_-]+)__(.+)$/);
  if (m) return { prefix: m[1], short: m[2] };
  return { short: full };
}

function ToolRow({
  name,
  description,
  disabled,
  onToggle,
}: {
  name: string;
  description?: string;
  disabled: boolean;
  onToggle: (enabled: boolean) => void;
}) {
  const { short, prefix } = shortToolName(name);
  return (
    <div className={`mcp-tool-row ${disabled ? "is-disabled" : ""}`}>
      <span className="mcp-tool-icon">
        <ToolGlyph />
      </span>
      <div className="mcp-tool-body">
        <div className="mcp-tool-name">
          <code>{short}</code>
          {prefix && <span className="mcp-tool-prefix">{prefix}</span>}
          {disabled && <span className="mcp-tool-tag">off</span>}
        </div>
        {description && <ClampedText>{description}</ClampedText>}
      </div>
      <button
        type="button"
        className={`mcp-tool-switch ${disabled ? "is-off" : "is-on"}`}
        title={disabled ? "Enable for agent" : "Hide from agent"}
        aria-pressed={!disabled}
        onClick={(e) => {
          e.stopPropagation();
          onToggle(disabled);
        }}
      >
        <span className="mcp-tool-switch-track">
          <span className="mcp-tool-switch-thumb" />
        </span>
      </button>
    </div>
  );
}

function ResourceRow({ resource }: { resource: McpResource }) {
  const meta = [resource.mime_type, resource.uri].filter(Boolean).join(" · ");
  return (
    <div className="mcp-tool-row">
      <span className="mcp-tool-icon">
        <FileGlyph />
      </span>
      <div className="mcp-tool-body">
        <div className="mcp-tool-name">
          <code>{resource.name || resource.uri}</code>
        </div>
        {resource.description && <ClampedText>{resource.description}</ClampedText>}
        {meta && <div className="mcp-tool-meta">{meta}</div>}
      </div>
    </div>
  );
}

function PromptRow({ prompt }: { prompt: McpPrompt }) {
  return (
    <div className="mcp-tool-row">
      <span className="mcp-tool-icon">
        <SparkGlyph />
      </span>
      <div className="mcp-tool-body">
        <div className="mcp-tool-name">
          <code>{prompt.name}</code>
          {prompt.arguments.length > 0 && (
            <span className="mcp-tool-args">({prompt.arguments.join(", ")})</span>
          )}
        </div>
        {prompt.description && <ClampedText>{prompt.description}</ClampedText>}
      </div>
    </div>
  );
}

function ServerEntry({
  server,
  busy,
  onToggle,
  onToolToggle,
}: {
  server: McpServer;
  busy: boolean;
  onToggle: () => void;
  onToolToggle: (tool: string, enabled: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const toolCount = server.toolNames.length;
  const extraCount = server.resources.length + server.prompts.length;
  const expandable = toolCount + extraCount > 0;
  const summary = [
    `${toolCount} tools`,
    server.resources.length ? `${server.resources.length} resources` : "",
    server.prompts.length ? `${server.prompts.length} prompts` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div style={{ borderBottom: "1px solid var(--border-soft)" }}>
      <div className="row" style={{ borderBottom: "none" }}>
        <div
          style={{ cursor: expandable ? "pointer" : "default", flex: 1 }}
          onClick={() => expandable && setOpen(!open)}
        >
          <div className="name">
            <span className={`tool-chevron ${open ? "open" : ""}`} style={{ display: "inline-flex" }}>
              {expandable ? <ChevronIcon /> : null}
            </span>{" "}
            <span className={`tool-status ${server.connected ? "done" : "error"}`} />{" "}
            {server.name} <span className="meta">· {server.transport}</span>
          </div>
          <div className="meta">
            {server.policyBlocked
              ? "blocked by policy"
              : server.connected
                ? `${summary} — click to expand`
                : server.error || "disconnected"}
          </div>
        </div>
        <button className="btn btn-sm" disabled={busy || server.policyBlocked} onClick={onToggle}>
          {busy ? "…" : server.connected ? "Disconnect" : "Connect"}
        </button>
      </div>
      {open && (
        <div className="mcp-server-body">
          {server.toolNames.length > 0 && (
            <div className="mcp-section">
              <div className="mcp-section-head">
                Tools <span className="mcp-section-count">{server.toolNames.length}</span>
              </div>
              <div className="mcp-tool-list">
                {server.toolNames.map((t) => (
                  <ToolRow
                    key={t}
                    name={t}
                    description={server.toolDescriptions[t]}
                    disabled={server.toolsDisabled.has(t)}
                    onToggle={(enabled) => onToolToggle(t, enabled)}
                  />
                ))}
              </div>
            </div>
          )}
          {server.resources.length > 0 && (
            <div className="mcp-section">
              <div className="mcp-section-head">
                Resources <span className="mcp-section-count">{server.resources.length}</span>
              </div>
              <div className="mcp-tool-list">
                {server.resources.map((r) => (
                  <ResourceRow key={r.uri} resource={r} />
                ))}
              </div>
            </div>
          )}
          {server.prompts.length > 0 && (
            <div className="mcp-section">
              <div className="mcp-section-head">
                Prompts <span className="mcp-section-count">{server.prompts.length}</span>
              </div>
              <div className="mcp-tool-list">
                {server.prompts.map((p) => (
                  <PromptRow key={p.name} prompt={p} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Seed for the composer — instead of building a form, let the agent
 *  collect the details and edit `.mcp.json` itself. Frames the task
 *  up front so the agent knows where each location lives and which
 *  JSON shape to write. */
const ADD_SERVER_PROMPT = `Help me set up a new MCP server.

Ask me which one I want to add (name, transport, command/args/env or URL) and which scope to save it under:
- **Project · shared** → \`./.mcp.json\` (committed, shared with the team)
- **Project · local** → \`./.igni/.mcp.json\` (gitignored, just this checkout)
- **User · global** → \`~/.igni/.mcp.json\` (this machine, all projects)

Then merge the entry into the chosen \`.mcp.json\` under the \`mcpServers\` key, creating the file if it doesn't exist. Use this shape:

\`\`\`json
{
  "mcpServers": {
    "<name>": { "type": "stdio", "command": "...", "args": [...], "env": {...} }
  }
}
\`\`\`

For SSE/HTTP servers, use \`"type": "sse"\` with a \`"url"\` field instead.

`;

export function McpPanel({
  client,
  onClose,
  onAddServer,
}: {
  client: EmberClient;
  onClose: () => void;
  onAddServer: (seed: string) => void;
}) {
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      await client.rpc("ensure_mcp");
      const details = await client.rpc<Record<string, unknown>[]>("get_mcp_server_details");
      setServers(
        (details || []).map((d) => ({
          name: String(d.name ?? "?"),
          connected: Boolean(d.connected),
          transport: String(d.transport ?? "unknown"),
          toolNames: (d.tool_names as string[]) || [],
          toolDescriptions: (d.tool_descriptions as Record<string, string>) || {},
          toolsDisabled: new Set<string>(((d.tools_disabled as string[]) || []) as string[]),
          resources: (d.resources as McpResource[]) || [],
          prompts: (d.prompts as McpPrompt[]) || [],
          error: d.error ? String(d.error) : undefined,
          policyBlocked: Boolean(d.policy_blocked),
        })),
      );
    } catch (e) {
      setServers([]);
      console.error(e);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const toggle = async (s: McpServer) => {
    setBusy(s.name);
    try {
      await client.mcpToggle(s.name, !s.connected);
    } finally {
      setBusy(null);
      void refresh();
    }
  };

  const toggleTool = async (serverName: string, tool: string, enabled: boolean) => {
    // Optimistic — flip the local set so the switch reacts instantly.
    setServers((prev) =>
      prev
        ? prev.map((s) => {
            if (s.name !== serverName) return s;
            const next = new Set(s.toolsDisabled);
            if (enabled) next.delete(tool);
            else next.add(tool);
            return { ...s, toolsDisabled: next };
          })
        : prev,
    );
    try {
      await client.rpc("set_mcp_tool_enabled", { server: serverName, tool, enabled });
    } catch (e) {
      console.error(e);
      void refresh();
    }
  };

  return (
    <Drawer
      title="MCP Servers"
      onClose={onClose}
      headerExtras={
        <div className="mcp-head-actions">
          <button
            className="btn btn-primary btn-sm"
            onClick={() => onAddServer(ADD_SERVER_PROMPT)}
          >
            + Add server
          </button>
        </div>
      }
    >
      {servers === null && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "10px 0",
                borderBottom: "1px solid var(--border-soft)",
              }}
            >
              <Skeleton.Circle size={10} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <Skeleton.Line width="35%" height={13} />
                <Skeleton.Line width="60%" height={11} style={{ marginTop: 4 }} />
              </div>
              <Skeleton.Block height={26} width={86} />
            </div>
          ))}
        </div>
      )}
      {servers?.length === 0 && <div className="msg-info">No MCP servers configured.</div>}
      {servers?.map((s) => (
        <ServerEntry
          key={s.name}
          server={s}
          busy={busy === s.name}
          onToggle={() => void toggle(s)}
          onToolToggle={(tool, enabled) => void toggleTool(s.name, tool, enabled)}
        />
      ))}
    </Drawer>
  );
}
