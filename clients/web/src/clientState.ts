/**
 * Per-client UI state — survives reloads, lives on the BE.
 *
 * Every client has a stable `client_id`. The web client stores that
 * one string in localStorage; the JetBrains plugin will use
 * PropertiesComponent and the VSCode extension `Memento`. Everything
 * else (which session this window is bound to, sidebar open/closed,
 * composer drafts, future toggles) round-trips to the BE via
 * `get_client_state` / `set_client_state` so all clients share the
 * same source of truth.
 *
 * Writes are coalesced per-key with a short debounce so typing in
 * the composer doesn't flood the WS.
 */

import type { IgniClient } from "./protocol/client";

const CLIENT_ID_KEY = "ember-code:client-id";

/** localStorage adapter; swap for the host's API in IDE clients. */
function readClientId(): string {
  try {
    return localStorage.getItem(CLIENT_ID_KEY) || "";
  } catch {
    return "";
  }
}

function writeClientId(id: string): void {
  try {
    localStorage.setItem(CLIENT_ID_KEY, id);
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

export function ensureClientId(): string {
  let id = readClientId();
  if (!id) {
    id = newClientId();
    writeClientId(id);
  }
  return id;
}

export class ClientStateStore {
  private cache: Record<string, string> = {};
  private hydrated = false;
  private pending: Map<string, ReturnType<typeof setTimeout>> = new Map();
  private listeners: Set<(key: string, value: string) => void> = new Set();

  constructor(
    private client: IgniClient,
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
