import { useCallback, useEffect, useMemo, useState } from "react";
import type { EmberClient } from "../../protocol/client";
import { Drawer } from "./Drawer";

// ── Types ────────────────────────────────────────────────────────

interface BranchEntry {
  sha: string;
  is_head: boolean;
  size_bytes: number;
  last_used_at: string;
  branch_refs: string[];
}

interface CodeIndexStatus {
  local_sha: string;
  head_indexed: boolean;
  sync_in_progress: boolean;
  sync_progress_pct: number | null;
  sync_step: string;
  sync_reason: string;
  sync_error: string;
  install_state: string;
  repository_id: string;
  install_url: string;
  remote_url: string;
  commits_indexed: number;
  index_size_bytes: number;
  branches_indexed: BranchEntry[];
  last_sync_at: string;
  last_sync_stats: { items_upserted?: number; items_deleted?: number };
}

interface LanguageEntry {
  ext: string;
  count: number;
}

interface CommitEntry {
  sha: string;
  full_sha: string;
  subject: string;
  when: string;
  indexed: boolean;
}

interface HeadBreakdown {
  file_count: number;
  languages: LanguageEntry[];
  recent_commits: CommitEntry[];
  files_indexed: number;
  languages_indexed: Record<string, number>;
  error?: string;
}

interface ActivityEntry {
  ts: string;
  sha: string;
  skipped: boolean;
  succeeded: boolean;
  in_progress: boolean;
  reason: string;
  error: string;
  duration_ms: number;
  items_upserted: number;
  items_deleted: number;
}

type Tone = "muted" | "good" | "warn" | "bad";

// ── Helpers ──────────────────────────────────────────────────────

function gitProvider(remoteUrl: string): { name: string; appLabel: string } {
  const host = remoteUrl.match(/(?:https?:\/\/|git@)([^/:]+)/)?.[1]?.toLowerCase() || "";
  if (host.includes("gitlab")) return { name: "GitLab", appLabel: "GitLab" };
  if (host.includes("bitbucket")) return { name: "Bitbucket", appLabel: "Bitbucket" };
  if (host.includes("github")) return { name: "GitHub", appLabel: "GitHub" };
  return { name: "Git provider", appLabel: "Provider" };
}

function formatBytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatRelative(iso: string): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const secs = Math.floor((Date.now() - t) / 1000);
  if (secs < 5) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

function formatNumber(n: number): string {
  if (n < 1000) return String(n);
  if (n < 10_000) return (n / 1000).toFixed(1) + "k";
  if (n < 1_000_000) return Math.round(n / 1000) + "k";
  return (n / 1_000_000).toFixed(1) + "M";
}

const LANG_PALETTE = [
  "#f97316", "#3b82f6", "#10b981", "#a855f7", "#ec4899",
  "#facc15", "#06b6d4", "#84cc16", "#f43f5e", "#64748b",
];

// ── Inline icons ─────────────────────────────────────────────────

function IconWrap({ children }: { children: React.ReactNode }) {
  return <span className="codeindex-icon">{children}</span>;
}

const Icons = {
  branch: () => (
    <IconWrap>
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none">
        <circle cx="4" cy="3.5" r="1.5" stroke="currentColor" strokeWidth="1.2" />
        <circle cx="4" cy="12.5" r="1.5" stroke="currentColor" strokeWidth="1.2" />
        <circle cx="12" cy="6.5" r="1.5" stroke="currentColor" strokeWidth="1.2" />
        <path d="M4 5v6M4 8c0-1.66 1.34-3 3-3h2c1.66 0 3-1.34 3-3" stroke="currentColor" strokeWidth="1.2" />
      </svg>
    </IconWrap>
  ),
  disk: () => (
    <IconWrap>
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none">
        <ellipse cx="8" cy="4" rx="5.5" ry="2" stroke="currentColor" strokeWidth="1.2" />
        <path d="M2.5 4v8a5.5 2 0 0 0 11 0V4" stroke="currentColor" strokeWidth="1.2" />
        <path d="M2.5 8a5.5 2 0 0 0 11 0" stroke="currentColor" strokeWidth="1.2" />
      </svg>
    </IconWrap>
  ),
  clock: () => (
    <IconWrap>
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none">
        <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.2" />
        <path d="M8 4.5v4l2.5 1.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
      </svg>
    </IconWrap>
  ),
  plug: () => (
    <IconWrap>
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none">
        <path d="M6 1.5v3M10 1.5v3M5 4.5h6v3a3 3 0 0 1-6 0v-3zM8 10.5v4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
      </svg>
    </IconWrap>
  ),
  coverage: () => (
    <IconWrap>
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none">
        <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.2" />
        <path d="M8 2a6 6 0 0 1 6 6h-6V2z" fill="currentColor" opacity="0.85" />
      </svg>
    </IconWrap>
  ),
};

// ── Main panel ───────────────────────────────────────────────────

function stateSummary(s: CodeIndexStatus): { label: string; tone: Tone; hint?: string } {
  if (s.sync_error) return { label: "Error", tone: "bad", hint: s.sync_error };
  if (s.install_state === "needs_install") {
    const provider = gitProvider(s.remote_url);
    return {
      label: "Not connected",
      tone: "warn",
      hint: `Install the ${provider.name} App to enable CodeIndex for this repo`,
    };
  }
  if (s.install_state === "inactive")
    return { label: "Inactive", tone: "muted", hint: "CodeIndex is disabled for this repository" };
  if (s.sync_in_progress) {
    const pct = s.sync_progress_pct != null ? ` · ${s.sync_progress_pct}%` : "";
    return { label: `Syncing${pct}`, tone: "warn", hint: s.sync_step || "Indexing HEAD…" };
  }
  if (s.head_indexed) return { label: "Up to date", tone: "good", hint: "HEAD is fully indexed and searchable" };
  return { label: "Out of date", tone: "warn", hint: s.sync_reason || "HEAD has not been indexed yet" };
}

export function CodeIndexPanel({
  client,
  onClose,
}: {
  client: EmberClient;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<CodeIndexStatus | null>(null);
  const [breakdown, setBreakdown] = useState<HeadBreakdown | null>(null);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [runningVerb, setRunningVerb] = useState<"sync" | "resync" | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await client.rpc<CodeIndexStatus>("codeindex_status"));
    } catch (e) {
      console.error(e);
    }
    try {
      setActivity((await client.rpc<ActivityEntry[]>("codeindex_activity")) || []);
    } catch {
      /* old BE */
    }
  }, [client]);

  const refreshBreakdown = useCallback(async () => {
    try {
      setBreakdown(await client.rpc<HeadBreakdown>("codeindex_head_breakdown"));
    } catch {
      setBreakdown(null);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
    void refreshBreakdown();
    const t = setInterval(refresh, 2_000);
    return () => clearInterval(t);
  }, [refresh, refreshBreakdown]);

  useEffect(() => {
    if (status && !status.sync_in_progress && runningVerb) {
      setRunningVerb(null);
      void refreshBreakdown();
    }
  }, [status, runningVerb, refreshBreakdown]);

  const act = async (verb: string) => {
    setBusy(verb);
    if (verb === "sync" || verb === "resync") setRunningVerb(verb);
    try {
      await client.rpc(
        `codeindex_${verb}`,
        verb === "sync" || verb === "resync" ? { sha: null } : {},
      );
    } catch (e) {
      console.error(e);
      if (verb === "sync" || verb === "resync") setRunningVerb(null);
    } finally {
      setBusy(null);
      void refresh();
    }
  };

  if (!status) {
    return (
      <Drawer title="CodeIndex" onClose={onClose}>
        <CodeIndexSkeleton />
      </Drawer>
    );
  }

  const s = stateSummary(status);
  const pct = status.sync_progress_pct;
  const needsInstall = status.install_state === "needs_install";
  const provider = gitProvider(status.remote_url);
  const lastDelta = status.last_sync_stats || {};

  // Recommended action banner — only when there's something concrete
  // for the user to do that isn't already obvious from the hero.
  let actionBanner: { tone: Tone; text: string; cta?: { label: string; onClick: () => void } } | null = null;
  if (needsInstall && status.install_url) {
    actionBanner = {
      tone: "warn",
      text: `Connect the ${provider.name} App so the agent can search this repo.`,
      cta: {
        label: `Connect ${provider.name}`,
        onClick: () => window.open(status.install_url, "_blank", "noopener"),
      },
    };
  } else if (!status.head_indexed && !status.sync_in_progress && !needsInstall) {
    const aheadCommits = (breakdown?.recent_commits || []).filter((c) => !c.indexed).length;
    actionBanner = {
      tone: "warn",
      text:
        aheadCommits > 0
          ? `${aheadCommits} recent commit(s) not indexed — sync to refresh.`
          : "HEAD isn't indexed — sync to enable code search.",
      cta: { label: "Sync now", onClick: () => void act("sync") },
    };
  }

  const actionRow = (
    <div className="codeindex-toolbar">
      <button
        className="btn btn-sm"
        disabled={!!busy || status.sync_in_progress || needsInstall}
        onClick={() => act("sync")}
        title={needsInstall ? "Connect the App first" : "Index commits since the last sync"}
      >
        {runningVerb === "sync" ? "Syncing…" : "Sync"}
      </button>
      <button
        className="btn btn-sm"
        disabled={!!busy || status.sync_in_progress || needsInstall}
        onClick={() => act("resync")}
        title={needsInstall ? "Connect the App first" : "Re-index the current HEAD from scratch"}
      >
        {runningVerb === "resync" ? "Resyncing…" : "Resync"}
      </button>
      <button
        className="btn btn-sm"
        disabled={!!busy || needsInstall}
        onClick={() => act("clean")}
        title={needsInstall ? "Connect the App first" : "Drop unused commit indexes"}
      >
        {busy === "clean" ? "Cleaning…" : "Clean"}
      </button>
    </div>
  );

  const coverageRaw =
    breakdown && breakdown.file_count > 0
      ? Math.round((breakdown.files_indexed / breakdown.file_count) * 100)
      : null;
  // A stale local index can include files that were renamed or
  // deleted since indexing, pushing this above 100%. Clamp for the
  // display; the raw counts in the sub-line still reveal the drift.
  const coveragePct = coverageRaw == null ? null : Math.min(100, coverageRaw);
  const coverageStale = coverageRaw != null && coverageRaw > 100;

  return (
    <Drawer title="CodeIndex" onClose={onClose} headerExtras={actionRow}>
      {/* ── Hero with status ring ──────────────────────────────── */}
      <Hero status={status} summary={s} provider={provider} />

      {status.sync_in_progress && pct != null && (
        <div className="codeindex-progress">
          <div className="codeindex-progress-track">
            <div className="codeindex-progress-fill" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}

      {actionBanner && (
        <div className={`codeindex-banner tone-${actionBanner.tone}`}>
          <span>{actionBanner.text}</span>
          {actionBanner.cta && (
            <button className="btn btn-primary btn-sm" onClick={actionBanner.cta.onClick}>
              {actionBanner.cta.label}
            </button>
          )}
        </div>
      )}

      {/* ── Stat tiles ───────────────────────────────────────────
       *  All of these (Coverage, Commits, On disk, Last sync,
       *  Connection state) are CodeIndex-specific telemetry. When
       *  the App isn't connected, the only meaningful one is the
       *  connection tile itself — keep that visible standalone so
       *  the user sees the actionable status, and skip the rest
       *  rather than render placeholder zeros. */}
      {needsInstall ? (
        <div className="codeindex-stats">
          <StatTile
            icon={<Icons.plug />}
            label={provider.appLabel}
            value="Not linked"
            tone="warn"
          />
        </div>
      ) : (
      <div className="codeindex-stats">
        <StatTile
          icon={<Icons.coverage />}
          label="Coverage"
          value={coveragePct != null ? `${coveragePct}%` : "—"}
          sub={
            breakdown && coveragePct != null
              ? coverageStale
                ? `${breakdown.files_indexed.toLocaleString()} / ${breakdown.file_count.toLocaleString()} · stale, resync`
                : `${breakdown.files_indexed.toLocaleString()} / ${breakdown.file_count.toLocaleString()} files`
              : "load breakdown…"
          }
          tone={
            coveragePct == null
              ? undefined
              : coverageStale
                ? "warn"
                : coveragePct >= 80
                  ? "good"
                  : coveragePct >= 30
                    ? "warn"
                    : "bad"
          }
        />
        <StatTile
          icon={<Icons.branch />}
          label="Commits"
          value={formatNumber(status.commits_indexed)}
          sub={status.local_sha ? `HEAD ${status.local_sha.slice(0, 7)}` : undefined}
        />
        <StatTile
          icon={<Icons.disk />}
          label="On disk"
          value={formatBytes(status.index_size_bytes)}
          sub={(() => {
            const head = status.branches_indexed.find((b) => b.is_head);
            const others = status.commits_indexed - (head ? 1 : 0);
            if (head && others > 0) {
              return `HEAD ${formatBytes(head.size_bytes)} · ${others} older`;
            }
            if (head) return `HEAD only`;
            if (others > 0) return `${others} cached commits`;
            return undefined;
          })()}
        />
        <StatTile
          icon={<Icons.clock />}
          label="Last sync"
          value={formatRelative(status.last_sync_at)}
          sub={
            lastDelta.items_upserted || lastDelta.items_deleted
              ? `+${lastDelta.items_upserted || 0} / −${lastDelta.items_deleted || 0}`
              : undefined
          }
        />
        <StatTile
          icon={<Icons.plug />}
          label={provider.appLabel}
          value={
            status.install_state === "installed"
              ? "Connected"
              : status.install_state.replace(/_/g, " ")
          }
          tone={status.install_state === "installed" ? "good" : "muted"}
        />
      </div>
      )}

      {/* ── Activity sparkline ─────────────────────────────────── */
      /*  Activity entries describe past sync/resync/etc. ops; when
       *  the App isn't installed there's nothing to plot and the
       *  empty-ish strip just adds vertical noise. */}
      {!needsInstall && activity.length > 0 && (
        <ActivityStrip activity={activity} />
      )}

      {/* ── HEAD: language donut + commits timeline ─────────────── */
      /*  Suppressed when CodeIndex isn't actually installed for
       *  this repo. ``codeindex_head_breakdown`` is just a
       *  ``git ls-files`` + recent-commit log; it returns real
       *  file counts and commits regardless of install state, so
       *  rendering it here would show e.g. "543 files · 0% indexed"
       *  + a commits timeline next to a "Not connected" hero —
       *  the user reads that as live data when it's actually
       *  meaningless until they connect the App. */}
      {!needsInstall && breakdown && !breakdown.error && breakdown.file_count > 0 && (
        <Section
          title="At HEAD"
          subtitle={
            coveragePct != null
              ? `${breakdown.file_count.toLocaleString()} files · ${coveragePct}% indexed`
              : `${breakdown.file_count.toLocaleString()} tracked files`
          }
        >
          <div className="codeindex-head-grid">
            <LanguageDonut
              languages={breakdown.languages}
              total={breakdown.file_count}
              indexed={breakdown.languages_indexed || {}}
              filesIndexed={breakdown.files_indexed}
            />
            <CommitTimeline commits={breakdown.recent_commits} />
          </div>
        </Section>
      )}

      {/* ── Cached commits ─────────────────────────────────────── */
      /*  Same reasoning as the other sections — if the index
       *  isn't installed there can't be cached commits, but if
       *  there's stale local data from a prior install we'd
       *  rather not surface it as if it were live. */}
      {!needsInstall && status.branches_indexed.length > 0 && (
        <Section title="Cached locally" subtitle={`${status.branches_indexed.length} commit(s)`}>
          <div className="codeindex-branches">
            {status.branches_indexed.slice(0, 6).map((b) => (
              <div key={b.sha} className={`codeindex-branch ${b.is_head ? "is-head" : ""}`}>
                <code className="codeindex-branch-sha">{b.sha.slice(0, 8)}</code>
                {b.is_head && <span className="codeindex-tag">HEAD</span>}
                {b.branch_refs.map((r) => (
                  <span key={r} className="codeindex-ref">{r}</span>
                ))}
                <span className="codeindex-branch-when">{formatRelative(b.last_used_at)}</span>
                <span className="codeindex-branch-size">{formatBytes(b.size_bytes)}</span>
              </div>
            ))}
            {status.branches_indexed.length > 6 && (
              <div className="codeindex-branch-more">+ {status.branches_indexed.length - 6} more</div>
            )}
          </div>
        </Section>
      )}

    </Drawer>
  );
}

// ── Hero with circular state indicator ───────────────────────────

function Hero({
  status,
  summary,
  provider,
}: {
  status: CodeIndexStatus;
  summary: { label: string; tone: Tone; hint?: string };
  provider: { name: string; appLabel: string };
}) {
  const pct = status.sync_progress_pct ?? (status.head_indexed ? 100 : 0);
  const tone = summary.tone;
  const repoLabel = status.repository_id || status.remote_url.split("/").slice(-2).join("/") || "this repo";

  return (
    <div className="codeindex-hero">
      <Ring percent={pct} tone={tone} />
      <div className="codeindex-hero-text">
        <div className="codeindex-state">{summary.label}</div>
        <div className="codeindex-hint">{summary.hint}</div>
        <div className="codeindex-hero-meta">
          <span className="codeindex-repo">{repoLabel}</span>
          {provider.name !== "Git provider" && (
            <span className="codeindex-provider">via {provider.name}</span>
          )}
        </div>
      </div>
    </div>
  );
}

function Ring({ percent, tone }: { percent: number; tone: Tone }) {
  const size = 68;
  const stroke = 6;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(100, percent));
  const offset = c - (clamped / 100) * c;
  const color =
    tone === "good" ? "#10b981" : tone === "warn" ? "#f59e0b" : tone === "bad" ? "#ef4444" : "#9ca3af";

  return (
    <svg className="codeindex-ring" width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        stroke="var(--bg-raised)"
        strokeWidth={stroke}
        fill="none"
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        stroke={color}
        strokeWidth={stroke}
        fill="none"
        strokeLinecap="round"
        strokeDasharray={c}
        strokeDashoffset={offset}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: "stroke-dashoffset 0.4s ease" }}
      />
      <text
        x="50%"
        y="50%"
        dominantBaseline="central"
        textAnchor="middle"
        fontSize="14"
        fontWeight="700"
        fill="var(--fg)"
      >
        {Math.round(clamped)}%
      </text>
    </svg>
  );
}

// ── Stat tile ────────────────────────────────────────────────────

function StatTile({
  icon,
  label,
  value,
  sub,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: string;
  tone?: Tone;
}) {
  return (
    <div className={`codeindex-tile ${tone ? `tone-${tone}` : ""}`}>
      <div className="codeindex-tile-head">
        {icon}
        <span className="codeindex-tile-label">{label}</span>
      </div>
      <div className="codeindex-tile-value">{value}</div>
      {sub && <div className="codeindex-tile-sub">{sub}</div>}
    </div>
  );
}

// ── Language donut chart ─────────────────────────────────────────

function LanguageDonut({
  languages,
  total,
  indexed,
  filesIndexed,
}: {
  languages: LanguageEntry[];
  total: number;
  indexed: Record<string, number>;
  filesIndexed: number;
}) {
  const [hover, setHover] = useState<string | null>(null);

  // Two concentric rings:
  //   outer (r=58) → total files per language
  //   inner (r=44) → indexed files per language (same angular span,
  //                  scaled by indexed/total so a fully-covered
  //                  language has a complete inner arc).
  const outerR = 58;
  const innerR = 44;
  const outerC = 2 * Math.PI * outerR;
  const innerC = 2 * Math.PI * innerR;

  const segments = useMemo(() => {
    let acc = 0;
    return languages.map((l, i) => {
      const fracTotal = l.count / total;
      // Clamp per-language indexed-count to the tracked count so a
      // stale index can't push the coverage past 100% (matches the
      // top-level Coverage tile's clamp).
      const indexedCount = Math.min(indexed[l.ext] || 0, l.count);
      const coverage = l.count > 0 ? indexedCount / l.count : 0;
      const outerLen = outerC * fracTotal;
      const innerLen = innerC * fracTotal * coverage;
      const seg = {
        ext: l.ext,
        count: l.count,
        indexed: indexedCount,
        coverage,
        color: LANG_PALETTE[i % LANG_PALETTE.length],
        outerDash: `${outerLen} ${outerC - outerLen}`,
        outerOffset: -outerC * acc + outerC * 0.25,
        innerDash: `${innerLen} ${innerC - innerLen}`,
        innerOffset: -innerC * acc + innerC * 0.25,
        pct: Math.round(fracTotal * 100),
      };
      acc += fracTotal;
      return seg;
    });
  }, [languages, total, indexed, outerC, innerC]);

  const focus = hover ? segments.find((s) => s.ext === hover) : null;
  const overallCoverage = total > 0 ? Math.round((filesIndexed / total) * 100) : 0;

  return (
    <div className="codeindex-donut-wrap">
      <svg width="148" height="148" viewBox="0 0 148 148" className="codeindex-donut">
        {/* Background tracks */}
        <circle cx="74" cy="74" r={outerR} fill="none" stroke="var(--bg-raised)" strokeWidth="10" />
        <circle
          cx="74"
          cy="74"
          r={innerR}
          fill="none"
          stroke="var(--bg-raised)"
          strokeWidth="6"
          opacity="0.5"
        />
        {/* Outer: total per language */}
        {segments.map((s) => (
          <circle
            key={`outer-${s.ext}`}
            cx="74"
            cy="74"
            r={outerR}
            fill="none"
            stroke={s.color}
            strokeWidth="10"
            strokeDasharray={s.outerDash}
            strokeDashoffset={s.outerOffset}
            opacity={hover && hover !== s.ext ? 0.2 : 1}
            onMouseEnter={() => setHover(s.ext)}
            onMouseLeave={() => setHover(null)}
            style={{ transition: "opacity 0.15s ease", cursor: "pointer" }}
          />
        ))}
        {/* Inner: indexed per language (shorter when not fully covered) */}
        {segments.map((s) =>
          s.indexed > 0 ? (
            <circle
              key={`inner-${s.ext}`}
              cx="74"
              cy="74"
              r={innerR}
              fill="none"
              stroke={s.color}
              strokeWidth="6"
              strokeDasharray={s.innerDash}
              strokeDashoffset={s.innerOffset}
              opacity={hover && hover !== s.ext ? 0.2 : 0.95}
              onMouseEnter={() => setHover(s.ext)}
              onMouseLeave={() => setHover(null)}
              style={{ transition: "opacity 0.15s ease", cursor: "pointer" }}
            />
          ) : null,
        )}
        {/* Center label */}
        <text x="74" y="68" textAnchor="middle" fontSize="20" fontWeight="700" fill="var(--fg)">
          {focus ? `${Math.round(focus.coverage * 100)}%` : `${overallCoverage}%`}
        </text>
        <text x="74" y="86" textAnchor="middle" fontSize="10.5" fill="var(--fg-faint)">
          {focus ? `.${focus.ext === "(other)" ? "·" : focus.ext} indexed` : "indexed"}
        </text>
      </svg>
      <ul className="codeindex-donut-legend">
        {segments.map((s) => {
          const cov = Math.round(s.coverage * 100);
          return (
            <li
              key={s.ext}
              className={hover === s.ext ? "is-hover" : ""}
              onMouseEnter={() => setHover(s.ext)}
              onMouseLeave={() => setHover(null)}
              title={`${s.indexed.toLocaleString()} of ${s.count.toLocaleString()} indexed`}
            >
              <span className="codeindex-donut-swatch" style={{ background: s.color }} />
              <span className="codeindex-donut-ext">.{s.ext === "(other)" ? "·" : s.ext}</span>
              <span className="codeindex-donut-count">{s.count.toLocaleString()}</span>
              <span
                className={`codeindex-donut-cov ${
                  cov >= 80 ? "tone-good" : cov >= 30 ? "tone-warn" : cov > 0 ? "tone-bad" : "tone-muted"
                }`}
              >
                {cov}%
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ── Commit timeline ──────────────────────────────────────────────

function CommitTimeline({ commits }: { commits: CommitEntry[] }) {
  if (commits.length === 0) return null;
  return (
    <div className="codeindex-timeline">
      {commits.map((c, i) => (
        <div key={c.full_sha} className="codeindex-timeline-row">
          <div className="codeindex-timeline-rail">
            <span className={`codeindex-timeline-node ${c.indexed ? "tone-good" : "tone-warn"}`} />
            {i < commits.length - 1 && <span className="codeindex-timeline-line" />}
          </div>
          <div className="codeindex-timeline-body">
            <div className="codeindex-timeline-top">
              <code>{c.sha}</code>
              <span className="codeindex-timeline-when">{c.when}</span>
            </div>
            <div className="codeindex-timeline-subj" title={c.subject}>
              {c.subject}
            </div>
            {!c.indexed && <div className="codeindex-timeline-tag">not indexed</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Activity sparkline ───────────────────────────────────────────

function ActivityStrip({ activity }: { activity: ActivityEntry[] }) {
  // Show newest on the right, oldest on the left so it reads
  // left-to-right like a timeline.
  const ordered = [...activity].reverse();
  const maxDuration = Math.max(...ordered.map((a) => a.duration_ms), 100);
  const [hover, setHover] = useState<number | null>(null);

  return (
    <div className="codeindex-section codeindex-section-compact">
      <div className="codeindex-section-head">
        <span className="codeindex-section-title">Activity</span>
        <span className="codeindex-section-sub">
          {hover != null ? formatRelative(ordered[hover].ts) : `last ${ordered.length}`}
        </span>
      </div>
      <div className="codeindex-spark">
        {ordered.map((a, i) => {
          const heightPct = Math.max(12, Math.round((a.duration_ms / maxDuration) * 100));
          const tone: Tone = a.error
            ? "bad"
            : a.skipped
              ? "muted"
              : a.in_progress
                ? "warn"
                : "good";
          return (
            <div
              key={`${a.ts}-${i}`}
              className={`codeindex-spark-bar tone-${tone} ${hover === i ? "is-hover" : ""}`}
              style={{ height: `${heightPct}%` }}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              title={`${a.ts}\n${a.error || a.reason || (a.succeeded ? `+${a.items_upserted} / −${a.items_deleted}` : "")}\n${a.duration_ms}ms`}
            />
          );
        })}
      </div>
      {hover != null && (
        <div className="codeindex-spark-detail">
          <span className="codeindex-spark-verb">
            {ordered[hover].error
              ? "Error"
              : ordered[hover].in_progress
                ? "In progress"
                : ordered[hover].skipped
                  ? "Skipped"
                  : "Synced"}
          </span>
          {ordered[hover].sha && (
            <code className="codeindex-spark-sha">{ordered[hover].sha.slice(0, 8)}</code>
          )}
          <span className="codeindex-spark-text">
            {ordered[hover].error ||
              ordered[hover].reason ||
              `+${ordered[hover].items_upserted} / −${ordered[hover].items_deleted}`}
          </span>
          <span className="codeindex-spark-when">{ordered[hover].duration_ms}ms</span>
        </div>
      )}
    </div>
  );
}

// ── Skeleton placeholders ────────────────────────────────────────

function CodeIndexSkeleton() {
  return (
    <>
      {/* Hero with ring placeholder */}
      <div className="codeindex-hero">
        <span
          className="skeleton skeleton-circle"
          style={{ width: 68, height: 68, flexShrink: 0 }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <span className="skeleton skeleton-text skeleton-line-mid" style={{ height: 18 }} />
          <span className="skeleton skeleton-text skeleton-line-wide" />
          <span className="skeleton skeleton-text skeleton-line-short" />
        </div>
      </div>

      {/* 5 stat tiles */}
      <div className="codeindex-stats">
        {Array.from({ length: 5 }).map((_, i) => (
          <span key={i} className="skeleton skeleton-block" />
        ))}
      </div>

      {/* Activity strip */}
      <div className="codeindex-section codeindex-section-compact">
        <span className="skeleton skeleton-text skeleton-line-short" />
        <span className="skeleton" style={{ height: 48, width: "100%" }} />
      </div>

      {/* HEAD grid */}
      <div className="codeindex-section">
        <span className="skeleton skeleton-text skeleton-line-short" />
        <div className="codeindex-head-grid">
          <span
            className="skeleton skeleton-circle"
            style={{ width: 148, height: 148 }}
          />
          <div>
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} style={{ marginBottom: 10 }}>
                <span className="skeleton skeleton-text skeleton-line-mid" />
                <span className="skeleton skeleton-text skeleton-line-wide" />
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}

// ── Section wrapper ──────────────────────────────────────────────

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="codeindex-section">
      <div className="codeindex-section-head">
        <span className="codeindex-section-title">{title}</span>
        {subtitle && <span className="codeindex-section-sub">{subtitle}</span>}
      </div>
      {children}
    </div>
  );
}
