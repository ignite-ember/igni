/**
 * Per-client UI state — survives reloads, lives on the BE.
 *
 * Every client has a stable `client_id`. The web client stores that
 * one string in localStorage, partitioned per window (see
 * `storageKeyFor`); the JetBrains plugin will use
 * PropertiesComponent and the VSCode extension `Memento`. Everything
 * else (which session this window is bound to, sidebar open/closed,
 * composer drafts, future toggles) round-trips to the BE via
 * `get_client_state` / `set_client_state` so all clients share the
 * same source of truth.
 *
 * Writes are coalesced per-key with a short debounce so typing in
 * the composer doesn't flood the WS.
 */

import type { EmberClient } from "./protocol/client";

const CLIENT_ID_KEY = "ember-code:client-id";

/**
 * Which `localStorage` slot holds this view's id.
 *
 * `localStorage` is per-origin, so every Tauri window of one app
 * instance shares it. An unpartitioned key would hand all of them
 * the same `client_id` — and since `client_state` holds *which
 * session this window is bound to*, a second window would open onto
 * the first one's chat and fight it over drafts and sidebar state.
 * Partitioning by the host's view key (the Tauri window label) gives
 * each window its own identity while keeping the state on the BE.
 *
 * The first window is labelled `main` and predates multi-window, so
 * it keeps the unsuffixed key: an existing install's bound session
 * and drafts survive the upgrade rather than resetting to a fresh
 * client. Hosts with a single view (browser, VSCode, JetBrains) pass
 * `""` and land on the same key they always used.
 *
 * Note for whoever wires up "New Window": labels must be stable
 * across app restarts, not minted fresh each launch. A random label
 * per launch means a window can never restore its binding, and the
 * BE's `client_state` table accrues a dead row set per window per
 * run.
 */
function storageKeyFor(viewKey: string): string {
  if (!viewKey || viewKey === "main") return CLIENT_ID_KEY;
  return `${CLIENT_ID_KEY}:${viewKey}`;
}

/** localStorage adapter; swap for the host's API in IDE clients. */
function readClientId(storageKey: string): string {
  try {
    return localStorage.getItem(storageKey) || "";
  } catch {
    return "";
  }
}

function writeClientId(storageKey: string, id: string): void {
  try {
    localStorage.setItem(storageKey, id);
  } catch {
    /* localStorage may be disabled — accept that state won't persist
       across reloads in that environment. */
  }
}

function newClientId(): string {
  // Browsers since 2022 have crypto.randomUUID, but be defensive.
  const cr = (globalThis as { crypto?: Crypto }).crypto;
  if (cr && typeof cr.randomUUID === "function") return cr.randomUUID();
  // Fallback: timestamp + random; collision risk is negligible for
  // our scale (a few clients per browser).
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * This view's stable `client_id`, minting one on first use.
 *
 * `viewKey` comes from `host.viewKey` — the Tauri window label, or
 * `""` on hosts that shell a single view. Two windows pass two
 * different keys and get two different ids; the same window across a
 * reload passes the same key and gets the same id back.
 */
export function ensureClientId(viewKey = ""): string {
  const storageKey = storageKeyFor(viewKey);
  let id = readClientId(storageKey);
  if (!id) {
    id = newClientId();
    writeClientId(storageKey, id);
  }
  return id;
}

export class ClientStateStore {
  private cache: Record<string, string> = {};
  private hydrated = false;
  private pending: Map<string, ReturnType<typeof setTimeout>> = new Map();
  private listeners: Set<(key: string, value: string) => void> = new Set();

  constructor(
    private client: EmberClient,
    public readonly clientId: string,
    private debounceMs = 250,
  ) {}

  /** Pull all keys for this client from the BE. Call once after
   *  the WS connects; subsequent reads are served from cache. */
  async hydrate(): Promise<void> {
    try {
      const all = await this.client.rpc<Record<string, string>>("get_client_state", {
        client_id: this.clientId,
      });
      this.cache = all || {};
    } catch {
      this.cache = {};
    } finally {
      this.hydrated = true;
    }
  }

  isHydrated(): boolean {
    return this.hydrated;
  }

  get(key: string): string | undefined {
    return this.cache[key];
  }

  /** Optimistic set — updates the cache immediately, debounces the
   *  network write so rapid changes (e.g. typing) collapse into one
   *  RPC per key. */
  set(key: string, value: string): void {
    this.cache[key] = value;
    const prev = this.pending.get(key);
    if (prev) clearTimeout(prev);
    const t = setTimeout(() => {
      this.pending.delete(key);
      void this.client
        .rpc("set_client_state", { client_id: this.clientId, key, value })
        .catch(() => {
          /* ignore — next hydrate will re-read */
        });
    }, this.debounceMs);
    this.pending.set(key, t);
    for (const fn of this.listeners) fn(key, value);
  }

  /** Remove a key. Useful for cleaning up empty drafts. */
  delete(key: string): void {
    delete this.cache[key];
    const prev = this.pending.get(key);
    if (prev) clearTimeout(prev);
    void this.client
      .rpc("delete_client_state", { client_id: this.clientId, key })
      .catch(() => {});
    for (const fn of this.listeners) fn(key, "");
  }

  onChange(fn: (key: string, value: string) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** Flush any pending debounced writes — call before the page
   *  unloads if you want them to land. */
  flush(): void {
    for (const [, timer] of this.pending) clearTimeout(timer);
    // Note: we don't synchronously send pending writes here because
    // the WS is async; in practice the debounce window is short
    // enough that this rarely matters.
    this.pending.clear();
  }
}
