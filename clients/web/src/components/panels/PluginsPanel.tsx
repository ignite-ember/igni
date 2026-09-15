import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import type { IgniClient } from "../../protocol/client";
import { Drawer } from "./Drawer";
import { Skeleton } from "../Skeleton";

interface PluginRow {
  name?: string;
  description?: string;
  version?: string;
  enabled?: boolean;
  source_root?: string;
  pin?: string;
  has_skills?: boolean;
  has_agents?: boolean;
  has_hooks?: boolean;
  has_mcp?: boolean;
  has_tools?: boolean;
}

interface MarketplacePlugin {
  name: string;
  source: string;
  description?: string;
  version?: string;
  branch?: string;
}

interface MarketplaceRow {
  name?: string;
  url?: string;
  last_fetched?: string;
  plugins?: MarketplacePlugin[];
}

interface PluginContents {
  name: string;
  root_path: string;
  skills: { name: string; description?: string }[];
  agents: { name: string; description?: string }[];
  hooks: { event: string; count: number }[];
  mcp_servers: { name: string; transport: string; command: string }[];
  tools: { name: string }[];
  readme: string;
  error?: string;
}

type Tab = "installed" | "marketplace";

// READMEs frequently use raw HTML for layout — center-aligned title
// blocks, `<picture>` with light/dark sources, shields.io badges,
// video thumbnails. We allow that markup but strip anything that
// could run code or exfiltrate (scripts, iframes, event handlers).
// Extending the rehype-sanitize default schema is the right surface
// for this — it already blocks <script>, <iframe>, event handlers,
// and javascript: URLs by default.
//
// Those four claims were checked by rendering payloads through this exact
// pipeline, and all four hold: `<script>` and `<iframe>` are dropped
// entirely, `onerror` is stripped off an `<img>` that survives, and
// `href="javascript:..."` becomes a bare `<a>`.
//
// Three things did NOT hold, and this is a third party's markup rendered
// inside a desktop app, so they matter more than they would on a web page:
//
//  1. `style` on `*` allowed a **full-viewport overlay**. Verified:
//     `<div style="position:fixed;top:0;left:0;width:100vw;height:100vh;
//     z-index:99999;background:#fff">Paste your token</div>` survived
//     intact. A marketplace listing could cover the entire app with
//     something that looks like the app's own dialog. `defaultSchema`
//     excludes `style` deliberately; extending it put that back.
//
//  2. `style` also allowed **outbound requests**: `background-image:
//     url(https://evil.example/pixel.png)` survived. In this product that
//     is not a privacy footnote — self-hosting.md §8 is explicit that a
//     silent phone-home breaks the air-gap claim, and browsing a
//     marketplace should not tell anybody you did.
//
//  3. `rel` on `<a>` let a README ask for `rel="opener"` alongside
//     `target="_blank"`, re-enabling reverse tabnabbing that the browser
//     default prevents.
//
// So: no `style` anywhere, and `rel` is ours to set rather than the
// README's — see the `a` component override at the render site. `align`,
// `width`, `height`, `srcset` and `media` stay, and they are what the
// layouts in the comment above actually use.
//
// Remote `<img>` src is still allowed and still an outbound request. That
// one is a deliberate trade-off rather than an oversight: shields.io
// badges are the single most common thing in a real README, and blocking
// them makes every listing look broken. It is scoped — an image request
// leaks that a listing was viewed, where `style` leaked the same thing
// from any of a dozen elements and could also repaint the whole window.
export const README_SANITIZE_SCHEMA = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames || []),
    "picture",
    "source",
    "video",
    "audio",
    "track",
    "details",
    "summary",
    "kbd",
    "sub",
    "sup",
    "mark",
  ],
  attributes: {
    ...defaultSchema.attributes,
    "*": [...(defaultSchema.attributes?.["*"] || []), "align"],
    img: [
      ...(defaultSchema.attributes?.img || []),
      "align",
      "width",
      "height",
      "loading",
    ],
    // ``srcSet``, not ``srcset``. rehype-sanitize matches **hast property
    // names**, which are camelCase, so the lowercase spelling allowed
    // nothing and the attribute was stripped on every render. That made
    // `<picture>` light/dark sources — one of the four layouts this schema
    // was widened for — silently inert: the dark variant was dropped and
    // every reader got the `<img>` fallback. Both spellings are listed so
    // the intent survives a future reader.
    source: ["srcSet", "srcset", "media", "type"],
    a: [...(defaultSchema.attributes?.a || []), "target"],
  },
};

/** Anchors in a README point outward, so they open in a new context with
 *  `rel` we control. Allowing the README to supply `rel` let it ask for
 *  `opener`; setting it here means it cannot. */
export const README_COMPONENTS = {
  a: ({
    node: _node,
    ...props
  }: { node?: unknown } & React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a {...props} target="_blank" rel="noopener noreferrer" />
  ),
};

type Selection =
  | { kind: "installed"; row: PluginRow }
  | { kind: "marketplace"; market: string; row: MarketplacePlugin };

/** Plugins panel — list ↔ detail navigation with breadcrumb.
 *  Installed and Marketplace are two tabs; clicking any plugin
 *  routes to a dedicated detail page within the same drawer. */
export function PluginsPanel({
  client,
  onClose,
}: {
  client: IgniClient;
  onClose: () => void;
}) {
  const [plugins, setPlugins] = useState<PluginRow[] | null>(null);
  const [markets, setMarkets] = useState<MarketplaceRow[] | null>(null);
  const [tab, setTab] = useState<Tab>("marketplace");
  const [selected, setSelected] = useState<Selection | null>(null);
  const [view, setView] = useState<"list" | "markets">("list");
  const [installRef, setInstallRef] = useState("");
  const [marketUrl, setMarketUrl] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");

  const refresh = useCallback(async () => {
    try {
      const details = await client.rpc<PluginRow[] | Record<string, unknown>>(
        "get_plugin_details",
      );
      setPlugins(
        Array.isArray(details)
          ? details
          : ((details as Record<string, unknown>).plugins as PluginRow[]) || [],
      );
    } catch (e) {
      setError(String(e));
      setPlugins([]);
    }
    try {
      setMarkets((await client.rpc<MarketplaceRow[]>("get_marketplaces")) || []);
    } catch {
      setMarkets([]);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setError("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  };

  const installedByName = useMemo(() => {
    const s = new Set<string>();
    for (const p of plugins || []) if (p.name) s.add(p.name);
    return s;
  }, [plugins]);

  const catalog = useMemo(() => {
    const flat: Array<{ market: string; plugin: MarketplacePlugin }> = [];
    for (const m of markets || []) {
      for (const p of m.plugins || []) flat.push({ market: m.name || m.url || "", plugin: p });
    }
    const q = query.trim().toLowerCase();
    if (!q) return flat;
    return flat.filter(
      ({ plugin }) =>
        plugin.name.toLowerCase().includes(q) ||
        (plugin.description || "").toLowerCase().includes(q) ||
        plugin.source.toLowerCase().includes(q),
    );
  }, [markets, query]);

  const counts = {
    installed: plugins?.length || 0,
    catalog: (markets || []).reduce((n, m) => n + (m.plugins?.length || 0), 0),
    markets: (markets || []).length,
  };

  // ── Depth ──────────────────────────────────────────────────────
  // Three views share the title slot: the list, a plugin's detail, and
  // marketplace management. The latter two are one level down, and the
  // page trail draws the way back — this panel used to draw its own
  // breadcrumb here, which as a page meant two trails stacked.
  // ``name`` is optional on a marketplace row — a crumb reading
  // "undefined" is worse than one naming the thing generically.
  const leaf = selected
    ? (selected.row.name ?? "Plugin")
    : view === "markets"
      ? "Marketplaces"
      : null;
  const title = leaf ?? "Plugins";
  const levels = {
    labels: leaf ? [leaf] : [],
    // One level deep, so any truncation is a return to the list.
    onTruncate: () => {
      setSelected(null);
      setView("list");
    },
  };

  // ── Header extras: tabs only on the list view ─────────────────
  const headerExtras =
    selected || view === "markets" ? null : (
      <div className="plugins-tabs">
        <button
          className={`plugins-tab ${tab === "marketplace" ? "active" : ""}`}
          onClick={() => setTab("marketplace")}
        >
          Marketplace
          <span className="plugins-tab-count">{counts.catalog}</span>
        </button>
        <button
          className={`plugins-tab ${tab === "installed" ? "active" : ""}`}
          onClick={() => setTab("installed")}
        >
          Installed
          <span className="plugins-tab-count">{counts.installed}</span>
        </button>
      </div>
    );

  // ── Drawer toolbar: search + refresh + gear on the catalog list ─
  const drawerToolbar =
    !selected && view === "list" && tab === "marketplace" && markets !== null ? (
      <div className="plugins-toolbar">
        <input
          className="panel-input plugins-search"
          placeholder={`Search ${counts.catalog} plugins across ${counts.markets} marketplace${counts.markets === 1 ? "" : "s"}…`}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button
          className="btn btn-sm"
          disabled={busy === "refresh"}
          onClick={() =>
            void act("refresh", () => client.rpc("refresh_marketplaces", { name: null }))
          }
          title="Re-fetch marketplace catalogs"
        >
          {busy === "refresh" ? "…" : "Refresh"}
        </button>
        <button
          className="kb-icon-btn"
          onClick={() => setView("markets")}
          title="Manage marketplaces"
          aria-label="Manage marketplaces"
        >
          <svg viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
            <path
              d="M8 5.6a2.4 2.4 0 1 0 0 4.8 2.4 2.4 0 0 0 0-4.8z"
              stroke="currentColor"
              strokeWidth="1.2"
            />
            <path
              d="M13.5 8a5.6 5.6 0 0 0-.09-1.01l1.36-1.06-1.4-2.42-1.6.65a5.6 5.6 0 0 0-1.75-1.01L9.7.5h-2.8l-.32 1.65a5.6 5.6 0 0 0-1.75 1.01l-1.6-.65-1.4 2.42L3.19 6.99A5.6 5.6 0 0 0 3.1 8c0 .34.03.68.09 1.01L1.83 10.07l1.4 2.42 1.6-.65a5.6 5.6 0 0 0 1.75 1.01l.32 1.65h2.8l.32-1.65a5.6 5.6 0 0 0 1.75-1.01l1.6.65 1.4-2.42-1.36-1.06c.06-.33.09-.67.09-1.01z"
              stroke="currentColor"
              strokeWidth="1.1"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </div>
    ) : null;

  return (
    <Drawer
      title={title}
      levels={levels}
      onClose={onClose}
      headerExtras={headerExtras}
      toolbar={drawerToolbar}
    >
      {error && <div className="msg-error" style={{ marginBottom: 10 }}>{error}</div>}

      {selected ? (
        <PluginDetailPage
          client={client}
          selection={selected}
          plugins={plugins}
          busy={busy}
          onInstall={(ref) =>
            void act("install", () => client.rpc("install_plugin", { ref }))
          }
          onToggle={(p) =>
            void act(p.name || "", () =>
              client.rpc("set_plugin_enabled", { name: p.name, enabled: !p.enabled }),
            )
          }
          onRemove={(p) =>
            void act(p.name || "", async () => {
              await client.rpc("remove_plugin", { name: p.name });
              setSelected(null);
            })
          }
        />
      ) : view === "markets" ? (
        <MarketplacesPage
          markets={markets}
          marketUrl={marketUrl}
          onMarketUrlChange={setMarketUrl}
          busy={busy}
          onAddMarketplace={(url) =>
            void act("add-market", async () => {
              const r = await client.rpc<{ text?: string }>("add_marketplace", { url });
              const txt = (r && typeof r === "object" && "text" in r ? r.text : "") || "";
              if (/^(Failed|git error)/i.test(txt)) throw new Error(txt);
              setMarketUrl("");
            })
          }
          onRemoveMarketplace={(name) =>
            void act(`rm-market-${name}`, () =>
              client.rpc("remove_marketplace", { name }),
            )
          }
          onRefresh={() =>
            void act("refresh", () => client.rpc("refresh_marketplaces", { name: null }))
          }
        />
      ) : tab === "installed" ? (
        <InstalledTab
          plugins={plugins}
          installRef={installRef}
          onInstallRefChange={setInstallRef}
          busy={busy}
          onInstall={(ref) =>
            void act("install", async () => {
              await client.rpc("install_plugin", { ref });
              setInstallRef("");
            })
          }
          onOpen={(p) => setSelected({ kind: "installed", row: p })}
        />
      ) : (
        <MarketplaceTab
          catalog={catalog}
          markets={markets}
          installedByName={installedByName}
          busy={busy}
          onInstall={(ref) =>
            void act("install", () => client.rpc("install_plugin", { ref }))
          }
          onOpen={(market, p) =>
            setSelected({ kind: "marketplace", market, row: p })
          }
        />
      )}
    </Drawer>
  );
}

// ── List tabs ─────────────────────────────────────────────────────

function InstalledTab({
  plugins,
  installRef,
  onInstallRefChange,
  busy,
  onInstall,
  onOpen,
}: {
  plugins: PluginRow[] | null;
  installRef: string;
  onInstallRefChange: (v: string) => void;
  busy: string;
  onInstall: (ref: string) => void;
  onOpen: (p: PluginRow) => void;
}) {
  if (plugins === null) return <PluginsSkeleton />;
  return (
    <>
      <div className="plugins-install-row">
        <input
          className="panel-input"
          placeholder="Install from URL: github.com/owner/repo or git URL…"
          value={installRef}
          onChange={(e) => onInstallRefChange(e.target.value)}
          onKeyDown={(e) =>
            e.key === "Enter" && installRef.trim() && onInstall(installRef.trim())
          }
        />
        <button
          className="btn btn-sm btn-primary"
          disabled={!installRef.trim() || busy === "install"}
          onClick={() => onInstall(installRef.trim())}
        >
          {busy === "install" ? "…" : "Install"}
        </button>
      </div>
      {plugins.length === 0 ? (
        <div className="plugins-empty">
          <div className="plugins-empty-title">No plugins installed yet</div>
          <div className="plugins-empty-hint">
            Open the Marketplace tab to browse and install plugins, or paste a
            git URL above.
          </div>
        </div>
      ) : (
        <div className="plugins-list">
          {plugins.map((p) => (
            <div
              className="plugins-card plugins-card-clickable"
              key={p.name}
              onClick={() => onOpen(p)}
            >
              <div className="plugins-card-head">
                <div className="plugins-card-title">
                  <span className={`plugins-status ${p.enabled ? "is-on" : "is-off"}`} />
                  <span className="plugins-name">{p.name}</span>
                  {p.version && <span className="plugins-version">{p.version}</span>}
                  {p.source_root && <span className="plugins-source">{p.source_root}</span>}
                </div>
                <span className="plugins-chevron-end">›</span>
              </div>
              {p.description && <div className="plugins-desc">{p.description}</div>}
              <PluginCapabilities p={p} />
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function MarketplaceTab({
  markets,
  catalog,
  installedByName,
  busy,
  onInstall,
  onOpen,
}: {
  markets: MarketplaceRow[] | null;
  catalog: Array<{ market: string; plugin: MarketplacePlugin }>;
  installedByName: Set<string>;
  busy: string;
  onInstall: (ref: string) => void;
  onOpen: (market: string, p: MarketplacePlugin) => void;
}) {
  if (markets === null) return <PluginsSkeleton />;
  return (
    <>
      {catalog.length === 0 ? (
        <div className="plugins-empty">
          <div className="plugins-empty-title">
            {markets.length === 0 ? "No marketplaces configured" : "No plugins match"}
          </div>
          <div className="plugins-empty-hint">
            {markets.length === 0
              ? "Add a marketplace URL or install by Git ref above."
              : "Try a different search, or refresh the catalogs."}
          </div>
        </div>
      ) : (
        <div className="plugins-list">
          {catalog.map(({ market, plugin }) => {
            const installed = installedByName.has(plugin.name);
            return (
              <div
                className="plugins-card plugins-card-clickable"
                key={`${market}:${plugin.name}`}
                onClick={() => onOpen(market, plugin)}
              >
                <div className="plugins-card-head">
                  <div className="plugins-card-title">
                    <span className="plugins-name">{plugin.name}</span>
                    {plugin.version && (
                      <span className="plugins-version">{plugin.version}</span>
                    )}
                    <span className="plugins-source">{market}</span>
                    {installed && <span className="plugins-tag">installed</span>}
                  </div>
                  {!installed && (
                    <div className="plugins-actions" onClick={(e) => e.stopPropagation()}>
                      <button
                        className="btn btn-sm btn-primary"
                        disabled={busy === plugin.name}
                        onClick={() => onInstall(plugin.source)}
                        title={`Install from ${plugin.source}`}
                      >
                        {busy === plugin.name ? "…" : "Install"}
                      </button>
                    </div>
                  )}
                  {installed && <span className="plugins-chevron-end">›</span>}
                </div>
                {plugin.description && (
                  <div className="plugins-desc">{plugin.description}</div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}

// ── Marketplaces management page ──────────────────────────────────

function MarketplacesPage({
  markets,
  marketUrl,
  onMarketUrlChange,
  busy,
  onAddMarketplace,
  onRemoveMarketplace,
  onRefresh,
}: {
  markets: MarketplaceRow[] | null;
  marketUrl: string;
  onMarketUrlChange: (v: string) => void;
  busy: string;
  onAddMarketplace: (url: string) => void;
  onRemoveMarketplace: (name: string) => void;
  onRefresh: () => void;
}) {
  if (markets === null) return <PluginsSkeleton />;
  return (
    <div className="plugins-markets-page">
      <div className="plugins-install-label">Add marketplace</div>
      <div className="plugins-section-hint">
        A marketplace is a git repo whose <code>.claude-plugin/marketplace.json</code>{" "}
        catalogues many plugins. Registering one lets you browse and install
        from its full catalog.
      </div>
      <div className="plugins-toolbar plugins-add-market">
        <input
          className="panel-input"
          placeholder="github.com/owner/repo or git URL…"
          value={marketUrl}
          onChange={(e) => onMarketUrlChange(e.target.value)}
          onKeyDown={(e) =>
            e.key === "Enter" && marketUrl.trim() && onAddMarketplace(marketUrl.trim())
          }
          autoFocus
        />
        <button
          className="btn btn-sm btn-primary"
          disabled={!marketUrl.trim() || busy === "add-market"}
          onClick={() => onAddMarketplace(marketUrl.trim())}
        >
          {busy === "add-market" ? "…" : "Add marketplace"}
        </button>
      </div>

      <div className="plugins-markets-list">
        <div className="plugins-markets-list-head">
          <span>Registered marketplaces ({markets.length})</span>
          <button
            className="btn btn-sm"
            disabled={busy === "refresh" || markets.length === 0}
            onClick={onRefresh}
            title="Re-fetch all marketplace catalogs"
          >
            {busy === "refresh" ? "…" : "Refresh all"}
          </button>
        </div>
        {markets.length === 0 ? (
          <div className="plugins-empty">
            <div className="plugins-empty-title">No marketplaces registered</div>
            <div className="plugins-empty-hint">
              Add one above to populate the catalog.
            </div>
          </div>
        ) : (
          markets.map((m) => {
            const name = m.name || m.url || "";
            const rmBusy = busy === `rm-market-${name}`;
            return (
              <div className="plugins-market" key={name}>
                <div className="plugins-market-main">
                  <div className="plugins-market-name">{name}</div>
                  <div className="plugins-market-url">{m.url}</div>
                  <div className="plugins-market-meta">
                    {(m.plugins || []).length} plugins
                    {m.last_fetched && ` · fetched ${m.last_fetched}`}
                  </div>
                </div>
                <button
                  className="btn btn-sm btn-danger"
                  disabled={rmBusy || !m.name}
                  onClick={() => onRemoveMarketplace(m.name || "")}
                  title={m.name ? `Unregister ${m.name}` : "Cannot remove (no name)"}
                >
                  {rmBusy ? "…" : "Remove"}
                </button>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// ── Detail page ───────────────────────────────────────────────────

function PluginDetailPage({
  client,
  selection,
  plugins,
  busy,
  onInstall,
  onToggle,
  onRemove,
}: {
  client: IgniClient;
  selection: Selection;
  plugins: PluginRow[] | null;
  busy: string;
  onInstall: (ref: string) => void;
  onToggle: (p: PluginRow) => void;
  onRemove: (p: PluginRow) => void;
}) {
  const [contents, setContents] = useState<PluginContents | null>(null);
  const [loading, setLoading] = useState(false);

  // Single source of truth for "is this plugin installed?" — look it
  // up in `plugins` by name so the detail page renders the same way
  // whether the user opened it from the Installed tab or from the
  // marketplace catalog (when they click into a plugin they already
  // have installed).
  const installedRow: PluginRow | undefined =
    selection.row.name && plugins
      ? plugins.find((p) => p.name === selection.row.name)
      : undefined;
  const isInstalled = !!installedRow;

  // Display data: prefer the installed row when present (it has the
  // canonical enabled/source_root/etc.); fall back to whatever the
  // marketplace catalog gave us.
  const name = installedRow?.name || selection.row.name || "";
  const description = installedRow?.description || selection.row.description;
  const version = installedRow?.version || selection.row.version;
  const sourceLabel =
    installedRow?.source_root ||
    (selection.kind === "marketplace" ? selection.market : "");

  // Contents fetch: read the installed copy when installed, else
  // preview from the marketplace ref.
  useEffect(() => {
    setContents(null);
    setLoading(true);
    let cancelled = false;
    const fetcher = isInstalled
      ? () => client.rpc<PluginContents>("get_plugin_contents", { name })
      : () =>
          client.rpc<PluginContents>("preview_plugin", {
            source: (selection.row as MarketplacePlugin).source,
            branch: (selection.row as MarketplacePlugin).branch || null,
            subdir: null,
          });
    void fetcher()
      .then((r) => {
        if (!cancelled) setContents(r);
      })
      .catch((e) => {
        if (!cancelled) {
          setContents({
            name,
            root_path: "",
            skills: [],
            agents: [],
            hooks: [],
            mcp_servers: [],
            tools: [],
            readme: "",
            error: e instanceof Error ? e.message : String(e),
          });
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [client, selection, isInstalled, name]);

  return (
    <div className="plugin-detail">
      <div className="plugin-detail-hero">
        <div className="plugin-detail-title-row">
          {isInstalled && (
            <span
              className={`plugins-status ${installedRow!.enabled ? "is-on" : "is-off"}`}
            />
          )}
          <h2 className="plugin-detail-title">{name}</h2>
          {version && <span className="plugins-version">{version}</span>}
          {sourceLabel && <span className="plugins-source">{sourceLabel}</span>}
        </div>
        {description && <div className="plugin-detail-desc">{description}</div>}
        <div className="plugin-detail-actions">
          {isInstalled ? (
            <>
              <button
                className="btn btn-sm"
                disabled={busy === name}
                onClick={() => onToggle(installedRow!)}
              >
                {installedRow!.enabled ? "Disable" : "Enable"}
              </button>
              <button
                className="btn btn-sm btn-danger"
                disabled={busy === name}
                onClick={() => onRemove(installedRow!)}
              >
                Remove
              </button>
            </>
          ) : (
            <button
              className="btn btn-sm btn-primary"
              disabled={busy === name}
              onClick={() =>
                onInstall((selection.row as MarketplacePlugin).source)
              }
              title={`Install from ${(selection.row as MarketplacePlugin).source}`}
            >
              {busy === name ? "Installing…" : "Install"}
            </button>
          )}
        </div>
      </div>

      <PluginContentsView
        contents={contents}
        loading={loading}
        showPath
        sourceFallback={
          !isInstalled && selection.kind === "marketplace"
            ? {
                source: selection.row.source,
                branch: selection.row.branch || "",
              }
            : null
        }
      />
    </div>
  );
}

function PluginContentsView({
  contents,
  loading,
  showPath,
  sourceFallback,
}: {
  contents: PluginContents | null;
  loading: boolean;
  showPath: boolean;
  sourceFallback: { source: string; branch: string } | null;
}) {
  if (loading || !contents) {
    return (
      <div className="plugins-contents">
        <Skeleton.Line width="35%" />
        <Skeleton.Line width="60%" style={{ marginTop: 8 }} />
        <Skeleton.Line width="40%" style={{ marginTop: 8 }} />
        <Skeleton.Line width="80%" style={{ marginTop: 14 }} />
        <Skeleton.Line width="70%" style={{ marginTop: 6 }} />
      </div>
    );
  }
  if (contents.error) {
    return <div className="plugins-contents msg-error">{contents.error}</div>;
  }
  const empty =
    contents.skills.length === 0 &&
    contents.agents.length === 0 &&
    contents.hooks.length === 0 &&
    contents.mcp_servers.length === 0 &&
    contents.tools.length === 0 &&
    !contents.readme;

  return (
    <div className="plugins-contents">
      {showPath && contents.root_path && (
        <div className="plugins-content-path">
          <span className="plugins-content-label">
            {sourceFallback ? "Source" : "Path"}
          </span>
          {sourceFallback ? (
            <SourceLink raw={contents.root_path} />
          ) : (
            <code>{contents.root_path}</code>
          )}
        </div>
      )}
      {sourceFallback?.branch && (
        <div className="plugins-content-path">
          <span className="plugins-content-label">Branch</span>
          <code>{sourceFallback.branch}</code>
        </div>
      )}

      {/* Skills hidden — README covers them in context. */}

      {contents.agents.length > 0 && (
        <ContentSection label="Agents" count={contents.agents.length}>
          {contents.agents.map((a) => (
            <div key={a.name} className="plugins-content-row">
              <code className="plugins-content-name">{a.name}</code>
              {a.description && (
                <span className="plugins-content-desc">{a.description}</span>
              )}
            </div>
          ))}
        </ContentSection>
      )}

      {contents.mcp_servers.length > 0 && (
        <ContentSection label="MCP servers" count={contents.mcp_servers.length}>
          {contents.mcp_servers.map((m) => (
            <div key={m.name} className="plugins-content-row">
              <code className="plugins-content-name">{m.name}</code>
              <span className="plugins-content-meta">{m.transport}</span>
              {m.command && <code className="plugins-content-cmd">{m.command}</code>}
            </div>
          ))}
        </ContentSection>
      )}

      {contents.hooks.length > 0 && (
        <ContentSection label="Hooks" count={contents.hooks.length}>
          {contents.hooks.map((h) => (
            <div key={h.event} className="plugins-content-row">
              <code className="plugins-content-name">{h.event}</code>
              <span className="plugins-content-meta">
                {h.count} handler{h.count === 1 ? "" : "s"}
              </span>
            </div>
          ))}
        </ContentSection>
      )}

      {contents.tools.length > 0 && (
        <ContentSection label="Tools" count={contents.tools.length}>
          {contents.tools.map((t) => (
            <div key={t.name} className="plugins-content-row">
              <code className="plugins-content-name">{t.name}</code>
            </div>
          ))}
        </ContentSection>
      )}

      {contents.readme && (
        <ContentSection label="README" count={null}>
          <div className="plugins-readme markdown-body">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[
                rehypeRaw,
                [rehypeSanitize, README_SANITIZE_SCHEMA],
                rehypeHighlight,
              ]}
              components={README_COMPONENTS}
            >
              {contents.readme}
            </ReactMarkdown>
          </div>
        </ContentSection>
      )}

      {empty && (
        <div className="plugins-empty-hint">
          This plugin doesn't bundle any skills, agents, hooks, MCP servers, or tools.
        </div>
      )}
    </div>
  );
}

/** Render the marketplace source string as a clickable URL plus an
 *  optional subdir chip — instead of one wall of monospace text. The
 *  catalog encodes subdir installs as ``<url> [<subdir>]``; we split
 *  that back out so the URL is clickable and the subdir is a chip. */
function SourceLink({ raw }: { raw: string }) {
  const m = raw.match(/^(.+?)\s+\[(.+?)\]\s*$/);
  const url = m ? m[1].trim() : raw.trim();
  const subdir = m ? m[2].trim() : "";
  // Strip ``.git`` suffix for cleaner display + drop a clickable
  // GitHub link (or whatever Git host) since the URL is the same
  // shape the user pastes into a browser.
  const displayUrl = url.replace(/\.git$/, "");
  const isHttp = /^https?:\/\//.test(url);
  return (
    <span className="plugins-source-link">
      {isHttp ? (
        <a href={url} target="_blank" rel="noreferrer">
          {displayUrl}
        </a>
      ) : (
        <code>{displayUrl}</code>
      )}
      {subdir && <span className="plugins-source-subdir">{subdir}</span>}
    </span>
  );
}

function ContentSection({
  label,
  count,
  children,
}: {
  label: string;
  count: number | null;
  children: React.ReactNode;
}) {
  return (
    <div className="plugins-content-section">
      <div className="plugins-content-head">
        <span>{label}</span>
        {count != null && <span className="plugins-content-count">{count}</span>}
      </div>
      <div>{children}</div>
    </div>
  );
}

function PluginCapabilities({ p }: { p: PluginRow }) {
  const caps = [
    p.has_skills && "skills",
    p.has_agents && "agents",
    p.has_hooks && "hooks",
    p.has_mcp && "mcp",
    p.has_tools && "tools",
  ].filter(Boolean) as string[];
  if (caps.length === 0) return null;
  return (
    <div className="plugins-caps">
      {caps.map((c) => (
        <span key={c} className="plugins-cap">{c}</span>
      ))}
      {p.pin && <span className="plugins-pin" title="Pinned ref">@{p.pin.slice(0, 8)}</span>}
    </div>
  );
}

function PluginsSkeleton() {
  return (
    <div className="plugins-list">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="plugins-card">
          <div className="plugins-card-head">
            <div style={{ flex: 1 }}>
              <Skeleton.Line width="40%" height={13} />
              <Skeleton.Line width="65%" height={11} style={{ marginTop: 6 }} />
            </div>
            <Skeleton.Block height={26} width={86} />
          </div>
          <Skeleton.Line width="90%" style={{ marginTop: 6 }} />
        </div>
      ))}
    </div>
  );
}
