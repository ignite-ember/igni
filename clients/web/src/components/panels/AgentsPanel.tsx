import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { IgniClient } from "../../protocol/client";
import { Drawer } from "./Drawer";
import { ChevronIcon } from "../Icons";

interface AgentRow {
  name?: string;
  description?: string;
  model?: string;
  is_ephemeral?: boolean;
  can_orchestrate?: boolean;
  tools?: string[];
  tags?: string[];
  mcp_servers?: string[];
  system_prompt?: string;
  source_path?: string;
}

function Tags({ items }: { items?: string[] }) {
  if (!items?.length) return null;
  return (
    <span className="agent-tags">
      {items.map((t) => (
        <span key={t} className="mini-tag">
          {t}
        </span>
      ))}
    </span>
  );
}

function AgentDetail({ agent: a }: { agent: AgentRow }) {
  // The drawer body keeps its scroll position across view switches —
  // start the detail page at the top.
  useEffect(() => {
    document.querySelector(".drawer-body")?.scrollTo(0, 0);
  }, [a.name]);
  return (
    <div>
      <div style={{ margin: "2px 0 14px" }}>
        {a.is_ephemeral && <span className="mini-tag ephemeral" style={{ marginLeft: 0, marginRight: 6 }}>ephemeral</span>}
        {a.can_orchestrate && <span className="mini-tag" style={{ marginLeft: 0, marginRight: 6 }}>orchestrates</span>}
        {a.model && <span className="mini-tag" style={{ marginLeft: 0, marginRight: 6 }}>{a.model}</span>}
        {a.description && <p className="meta" style={{ margin: "6px 0 0" }}>{a.description}</p>}
      </div>
      {!!a.tools?.length && (
        <div className="agent-kv">
          <span className="agent-kv-label">Tools</span>
          <Tags items={a.tools} />
        </div>
      )}
      {!!a.mcp_servers?.length && (
        <div className="agent-kv">
          <span className="agent-kv-label">MCP</span>
          <Tags items={a.mcp_servers} />
        </div>
      )}
      {!!a.tags?.length && (
        <div className="agent-kv">
          <span className="agent-kv-label">Tags</span>
          <Tags items={a.tags} />
        </div>
      )}
      {a.source_path && (
        <div className="agent-kv">
          <span className="agent-kv-label">Source</span>
          <span className="meta" style={{ wordBreak: "break-all" }}>
            {a.source_path}
          </span>
        </div>
      )}
      {a.system_prompt && (
        <>
          <div className="agent-kv-label" style={{ margin: "16px 0 6px" }}>
            System prompt
          </div>
          <div className="agent-prompt-md">
            <ReactMarkdown>{a.system_prompt}</ReactMarkdown>
          </div>
        </>
      )}
    </div>
  );
}

/** Agent pool: list → detail navigation with breadcrumb. Detail shows
 *  tools/tags/MCP and the system prompt rendered as markdown;
 *  ephemeral agents keep inline Keep/Discard in the list. */
export function AgentsPanel({
  client,
  onClose,
}: {
  client: IgniClient;
  onClose: () => void;
}) {
  const [agents, setAgents] = useState<AgentRow[] | null>(null);
  const [selected, setSelected] = useState<AgentRow | null>(null);
  const [busy, setBusy] = useState("");

  const refresh = useCallback(async () => {
    try {
      setAgents((await client.rpc<AgentRow[]>("get_agent_details")) || []);
    } catch (e) {
      console.error(e);
      setAgents([]);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const act = async (name: string, method: string) => {
    setBusy(name);
    try {
      await client.rpc(method, { name });
      await refresh();
    } finally {
      setBusy("");
    }
  };

  return (
    <Drawer
      title={
        selected ? (
          <span className="breadcrumb" style={{ margin: 0 }}>
            <button className="breadcrumb-link" onClick={() => setSelected(null)}>
              Agents
            </button>
            <span className="breadcrumb-sep">›</span>
            <strong>{selected.name}</strong>
          </span>
        ) : (
          "Agents"
        )
      }
      onClose={onClose}
    >
      {selected ? (
        <AgentDetail agent={selected} />
      ) : (
        <>
          {agents === null && <div className="msg-info">Loading…</div>}
          {agents?.map((a) => (
            <div className="agent-head" key={a.name} onClick={() => setSelected(a)}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="name">
                  {a.name}
                  {a.is_ephemeral && <span className="mini-tag ephemeral">ephemeral</span>}
                  {a.can_orchestrate && <span className="mini-tag">orchestrates</span>}
                  {a.model && <span className="mini-tag">{a.model}</span>}
                </div>
                <div className="meta">{a.description}</div>
              </div>
              {a.is_ephemeral && (
                <div style={{ display: "flex", gap: 6 }} onClick={(e) => e.stopPropagation()}>
                  <button
                    className="btn btn-sm"
                    title="Keep this generated agent permanently"
                    disabled={busy === a.name}
                    onClick={() => void act(a.name || "", "promote_ephemeral_agent")}
                  >
                    Keep
                  </button>
                  <button
                    className="btn btn-sm btn-danger"
                    disabled={busy === a.name}
                    onClick={() => void act(a.name || "", "discard_ephemeral_agent")}
                  >
                    Discard
                  </button>
                </div>
              )}
              <span className="agent-open-chevron">
                <ChevronIcon size={11} />
              </span>
            </div>
          ))}
        </>
      )}
    </Drawer>
  );
}
