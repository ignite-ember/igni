/**
 * What the backend addresses to installed extensions' frames.
 *
 * The `host_signal` push: a host-model signal an extension's page
 * subscribed to, for the bridge's `on(signal, fn)`
 * (`docs/HOST_OBJECT_MODEL.md`). The backend already decided who was
 * entitled to hear each one; this only routes it.
 *
 * Kept as per-extension queues with a monotonic `n` rather than "the
 * latest one", because two pushes that land in the same React batch
 * collapse into one render, and a single slot drops whichever came
 * first. Bounded: a frame that is closed must not turn a chatty
 * producer into a growing array nobody reads.
 */

import { useEffect, useState } from "react";

import type { IgniClient } from "../protocol/client";

/** How many undelivered items one extension's queue keeps. */
export const EXTENSION_TRAFFIC_BACKLOG = 64;

export interface ExtensionSignalItem {
  signal: string;
  value: unknown;
  n: number;
}

function enqueue<T extends { n: number }>(
  current: Record<string, T[]>,
  id: string,
  item: Omit<T, "n">,
): Record<string, T[]> {
  const queue = current[id] ?? [];
  const next = { ...item, n: (queue[queue.length - 1]?.n ?? 0) + 1 } as T;
  return {
    ...current,
    [id]: [...queue, next].slice(-EXTENSION_TRAFFIC_BACKLOG),
  };
}

export function useExtensionTraffic(
  client: IgniClient,
  connected: boolean,
): {
  signals: Record<string, ExtensionSignalItem[]>;
} {
  const [signals, setSignals] = useState<
    Record<string, ExtensionSignalItem[]>
  >({});

  useEffect(() => {
    if (!connected) return;
    return client.onEvent((message) => {
      const push = message as unknown as {
        channel?: string;
        payload?: unknown;
      };
      const body = (push.payload ?? {}) as Record<string, unknown>;
      const id = body.extension_id;
      if (typeof id !== "string") return;
      if (push.channel === "host_signal") {
        if (typeof body.signal !== "string") return;
        setSignals((current) =>
          enqueue<ExtensionSignalItem>(current, id, {
            signal: body.signal as string,
            value: body.value,
          }),
        );
      }
    });
  }, [client, connected]);

  return { signals };
}
