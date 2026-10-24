/**
 * The floor's notifications, as the backend describes them.
 *
 * igni server's `docs/service-identity.md` §10. Two paths arrive at the
 * backend — what igni server delivered to whoever is signed in, and
 * what an extension's own process told this machine — and leave it as
 * one list and one count per extension. The client draws both and
 * computes neither: no extension keeps a "waiting" count of its own,
 * and neither does this.
 */
import { useEffect, useState } from "react";

import type { IgniClient } from "../protocol/client";

/** Where opening one goes: one of its extension's pages. */
export interface NotificationTarget {
  page: string;
  params: Record<string, string>;
}

/** One notification, from either path. */
export interface FloorNotification {
  id: string;
  /** `server`: read state lives on igni server. `local`: on this machine. */
  origin: "server" | "local";
  /** The extension it is from. */
  source: string;
  kind: string;
  title: string;
  body: string;
  /** Who did it — empty for a notice an extension sent about your own work. */
  actor: string;
  target: NotificationTarget | null;
  created_at: string | null;
  read: boolean;
  /** The organisation it happened in — server rows only. */
  org_id?: string;
  /** That organisation's name, sent only when the person belongs to more
   *  than one: with one, every row is from it. */
  org_name?: string;
}

/** Unread, in all and per extension. The badge on each extension. */
export interface NotificationCounts {
  unread: number;
  by_source: Record<string, number>;
}

export const NO_COUNTS: NotificationCounts = { unread: 0, by_source: {} };

/** The push the backend sends whenever either path moves the counts. */
export const COUNTS_CHANNEL = "notifications_changed";

/** Checked, not cast: this number is the one the app asks to be believed. */
export function isCounts(value: unknown): value is NotificationCounts {
  if (typeof value !== "object" || value === null) return false;
  const counts = value as Record<string, unknown>;
  return (
    typeof counts.unread === "number" &&
    typeof counts.by_source === "object" &&
    counts.by_source !== null
  );
}

/** One extension's unread count. */
export function unreadFor(
  counts: NotificationCounts,
  source: string | null | undefined,
): number {
  if (!source) return 0;
  const n = counts.by_source[source];
  return typeof n === "number" && n > 0 ? n : 0;
}

/** A count as a badge draws it. */
export function badgeText(n: number): string {
  return n > 99 ? "99+" : String(n);
}

/** The counts `notifications_changed` carries, or `null` for anything else. */
export function countsFromPush(message: unknown): NotificationCounts | null {
  const push = message as { channel?: unknown; payload?: { counts?: unknown } };
  if (push?.channel !== COUNTS_CHANNEL) return null;
  return isCounts(push.payload?.counts) ? push.payload.counts : null;
}

/**
 * The floor's counts, kept current by the backend's pushes.
 *
 * Asked once whenever `connected` becomes true — the backend answers
 * from what it last read and asks nobody — and after that moved only
 * by `notifications_changed`. Nothing here runs on a timer.
 */
export function useNotificationCounts(
  client: IgniClient,
  connected: boolean,
): [NotificationCounts, (counts: NotificationCounts) => void] {
  const [counts, setCounts] = useState<NotificationCounts>(NO_COUNTS);

  useEffect(() => {
    if (!connected) return;
    let alive = true;
    void client
      .rpc<unknown>("notifications_counts")
      .then((answer) => {
        if (alive && isCounts(answer)) setCounts(answer);
      })
      .catch(() => {
        // A backend that cannot say keeps the last number rather than
        // reading as "nothing is waiting".
      });
    return () => {
      alive = false;
    };
  }, [client, connected]);

  useEffect(() => {
    if (!connected) return;
    return client.onEvent((message) => {
      const next = countsFromPush(message);
      if (next) setCounts(next);
    });
  }, [client, connected]);

  return [counts, setCounts];
}
