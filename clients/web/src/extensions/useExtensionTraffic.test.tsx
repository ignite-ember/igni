// @vitest-environment jsdom
/**
 * The window's routing of what the backend addressed to extensions:
 * `extension_event` for `onEvent`, `host_signal` for `bridge.on`.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { IgniClient } from "../protocol/client";
import { EXTENSION_TRAFFIC_BACKLOG, useExtensionTraffic } from "./useExtensionTraffic";

afterEach(cleanup);

function fakeClient() {
  const listeners = new Set<(message: unknown) => void>();
  const client = {
    onEvent(fn: (message: unknown) => void) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  } as unknown as IgniClient;
  return {
    client,
    push(channel: string, payload: Record<string, unknown>) {
      act(() => {
        for (const fn of listeners) fn({ type: "push_notification", channel, payload });
      });
    },
  };
}

describe("useExtensionTraffic", () => {
  it("queues each extension's signals and events separately, in order", () => {
    const { client, push } = fakeClient();
    const { result } = renderHook(() => useExtensionTraffic(client, true));

    push("host_signal", { extension_id: "acme.one", signal: "chat.run_started", value: { run_id: "r1" } });
    push("host_signal", { extension_id: "acme.two", signal: "session.switched", value: {} });
    push("extension_event", { extension_id: "acme.one", topic: "ext.acme.one.done", payload: { n: 1 } });
    push("host_signal", { extension_id: "acme.one", signal: "chat.run_completed", value: {} });

    expect(result.current.signals["acme.one"].map((s) => [s.signal, s.n])).toEqual([
      ["chat.run_started", 1],
      ["chat.run_completed", 2],
    ]);
    expect(result.current.signals["acme.two"]).toHaveLength(1);
    expect(result.current.events["acme.one"]).toEqual([
      { topic: "ext.acme.one.done", payload: { n: 1 }, n: 1 },
    ]);
  });

  it("is bounded, so a closed frame does not grow a queue nobody reads", () => {
    const { client, push } = fakeClient();
    const { result } = renderHook(() => useExtensionTraffic(client, true));
    for (let i = 0; i < EXTENSION_TRAFFIC_BACKLOG + 5; i++) {
      push("host_signal", { extension_id: "acme.one", signal: "chat.content_delta", value: { i } });
    }
    const queue = result.current.signals["acme.one"];
    expect(queue).toHaveLength(EXTENSION_TRAFFIC_BACKLOG);
    expect(queue[queue.length - 1].n).toBe(EXTENSION_TRAFFIC_BACKLOG + 5);
  });

  it("ignores pushes with no extension to address", () => {
    const { client, push } = fakeClient();
    const { result } = renderHook(() => useExtensionTraffic(client, true));
    push("host_signal", { signal: "chat.run_started" });
    push("notifications_changed", { extension_id: "acme.one" });
    expect(result.current.signals).toEqual({});
    expect(result.current.events).toEqual({});
  });
});
