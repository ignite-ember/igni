// @vitest-environment jsdom
/**
 * The floor's counts: asked once on connect, moved only by the push.
 *
 * Every badge — the bell, each extension's sidebar entry, each
 * extension's chip — reads this one number. Nothing here runs on a
 * timer, and nothing here asks an extension.
 */
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  NO_COUNTS,
  badgeText,
  countsFromPush,
  unreadFor,
  useNotificationCounts,
  type NotificationCounts,
} from "./model";

afterEach(cleanup);

function backend(initial: NotificationCounts) {
  const listeners = new Set<(m: unknown) => void>();
  const rpc = vi.fn(async () => initial);
  const client = {
    rpc,
    onEvent: (fn: (m: unknown) => void) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
  const push = (message: unknown) => {
    for (const fn of listeners) fn(message);
  };
  return { client: client as never, rpc, push };
}

describe("the counts", () => {
  it("are asked for once when connected", async () => {
    const { client, rpc } = backend({ unread: 3, by_source: { "igni.artifacts": 3 } });

    const { result } = renderHook(() => useNotificationCounts(client, true));

    await waitFor(() => expect(result.current[0].unread).toBe(3));
    expect(rpc).toHaveBeenCalledTimes(1);
    expect(rpc).toHaveBeenCalledWith("notifications_counts");
  });

  it("are not asked for before there is a connection", () => {
    const { client, rpc } = backend({ unread: 3, by_source: {} });

    const { result } = renderHook(() => useNotificationCounts(client, false));

    expect(rpc).not.toHaveBeenCalled();
    expect(result.current[0]).toEqual(NO_COUNTS);
  });

  it("move with the push, and with nothing else", async () => {
    const { client, rpc, push } = backend({ unread: 0, by_source: {} });
    const { result } = renderHook(() => useNotificationCounts(client, true));
    await waitFor(() => expect(rpc).toHaveBeenCalledTimes(1));

    act(() =>
      push({
        type: "push_notification",
        channel: "notifications_changed",
        payload: { counts: { unread: 2, by_source: { "acme.scanner": 2 } } },
      }),
    );
    act(() => push({ type: "push_notification", channel: "host_signal", payload: {} }));

    expect(result.current[0]).toEqual({ unread: 2, by_source: { "acme.scanner": 2 } });
    expect(rpc).toHaveBeenCalledTimes(1);
  });
});

describe("reading them", () => {
  const counts = { unread: 5, by_source: { "igni.artifacts": 4, "acme.scanner": 1 } };

  it("gives each extension its own share", () => {
    expect(unreadFor(counts, "igni.artifacts")).toBe(4);
    expect(unreadFor(counts, "acme.scanner")).toBe(1);
    expect(unreadFor(counts, "acme.unknown")).toBe(0);
    // A base page has no owner, and no badge.
    expect(unreadFor(counts, null)).toBe(0);
  });

  it("refuses a push that is not counts", () => {
    expect(countsFromPush({ channel: "notifications_changed", payload: { counts: { unread: "3" } } })).toBeNull();
    expect(countsFromPush({ channel: "artifact_waiting", payload: { waiting: 3 } })).toBeNull();
  });

  it("caps what a badge draws", () => {
    expect(badgeText(7)).toBe("7");
    expect(badgeText(140)).toBe("99+");
  });
});
