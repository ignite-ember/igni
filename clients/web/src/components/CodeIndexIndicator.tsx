import { useEffect, useState } from "react";
import type { EmberClient } from "../protocol/client";

export interface CodeIndexStatus {
  head_indexed: boolean;
  sync_in_progress: boolean;
  sync_progress_pct: number | null;
  sync_error: string;
  install_state: string;
  remote_url: string;
}

export function providerName(remoteUrl: string): string {
  const host = remoteUrl.match(/(?:https?:\/\/|git@)([^/:]+)/)?.[1]?.toLowerCase() || "";
  if (host.includes("gitlab")) return "GitLab";
  if (host.includes("bitbucket")) return "Bitbucket";
  if (host.includes("github")) return "GitHub";
  return "Git provider";
}

type Tone = "muted" | "good" | "warn" | "bad";

interface BadgeState {
  label: string;
  tone: Tone;
  detail: string;
}

export function classify(s: CodeIndexStatus | null): BadgeState {
  if (!s) return { label: "checking…", tone: "muted", detail: "Probing CodeIndex" };
  if (s.sync_error) return { label: "error", tone: "bad", detail: s.sync_error };
  if (s.install_state === "needs_install")
    return {
      label: "uninstalled",
      tone: "warn",
      detail: `${providerName(s.remote_url)} repository not connected`,
    };
  if (s.install_state === "inactive")
    return { label: "inactive", tone: "muted", detail: "CodeIndex inactive for this repo" };
  if (s.sync_in_progress) {
    const pct = s.sync_progress_pct != null ? ` ${s.sync_progress_pct}%` : "…";
    return { label: `syncing${pct}`, tone: "warn", detail: "Indexing current HEAD" };
  }
  if (s.head_indexed) return { label: "indexed", tone: "good", detail: "HEAD is fully indexed" };
  return { label: "not indexed", tone: "warn", detail: "HEAD needs a sync" };
}

/** Always-visible CodeIndex state pill in the footer — TUI parity.
 *  Click opens the panel. Polls at 2s while open or while syncing. */
export function CodeIndexIndicator({
  client,
  onOpen,
}: {
  client: EmberClient;
  onOpen: () => void;
}) {
  const [status, setStatus] = useState<CodeIndexStatus | null>(null);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      try {
        const s = await client.rpc<CodeIndexStatus>("codeindex_status");
        if (live) setStatus(s);
      } catch {
        if (live) setStatus(null);
      }
    };
    void tick();
    // Slow when stable, fast (1.5s) when syncing.
    const interval = setInterval(tick, status?.sync_in_progress ? 1_500 : 5_000);
    return () => {
      live = false;
      clearInterval(interval);
    };
  }, [client, status?.sync_in_progress]);

  const b = classify(status);
  return (
    <button
      className={`codeindex-pill tone-${b.tone}`}
      title={b.detail}
      onClick={onOpen}
    >
      <span className="codeindex-dot" />
      CodeIndex <span className="codeindex-label">{b.label}</span>
    </button>
  );
}
