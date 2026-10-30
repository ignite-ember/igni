// @vitest-environment jsdom
/**
 * The floor's notifications panel: one list, every extension.
 *
 * It asks igni's backend, never an extension. Opening a row goes to the
 * extension page it names, with the params it carries, and marks it
 * read; the counts it learns go to every badge, because marking
 * something read here must not leave the bell (or an extension's
 * sidebar entry) reading a number with nothing behind it.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { NotificationsPanel } from "./NotificationsPanel";
import type { FloorNotification, NotificationCounts } from "../notifications/model";

afterEach(cleanup);

function row(over: Partial<FloorNotification> = {}): FloorNotification {
  return {
    id: "s1",
    origin: "server",
    source: "igni.artifacts",
    kind: "comment",
    title: "Runbook",
    body: "Ada: can we drop step 3?",
    actor: "ada@acme.com",
    target: { page: "igni.artifacts", params: { slug: "runbook", comment: "c1" } },
    created_at: new Date().toISOString(),
    read: false,
    ...over,
  };
}

function countsOf(rows: FloorNotification[]): NotificationCounts {
  const by_source: Record<string, number> = {};
  for (const r of rows) if (!r.read) by_source[r.source] = (by_source[r.source] ?? 0) + 1;
  return { unread: Object.values(by_source).reduce((a, b) => a + b, 0), by_source };
}

/** A backend holding `rows`, marking them read the way the real one does. */
function backendWith(initial: FloorNotification[]) {
  let rows = initial.map((r) => ({ ...r }));
  const calls: { method: string; args: Record<string, unknown> }[] = [];
  const listeners = new Set<(m: unknown) => void>();
  const client = {
    rpc: vi.fn(async (method: string, args: Record<string, unknown> = {}) => {
      calls.push({ method, args });
      if (method === "notifications_mark_read") {
        const ids = args.ids as string[] | undefined;
        rows = rows.map((r) => (!ids || ids.includes(r.id) ? { ...r, read: true } : r));
        return { counts: countsOf(rows), server_error: "" };
      }
      if (method === "notifications_list") {
        return { notifications: rows, counts: countsOf(rows), server_error: "" };
      }
      throw new Error(`unexpected ${method}`);
    }),
    onEvent: (fn: (m: unknown) => void) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
  const push = (payload: unknown) => {
    for (const fn of listeners) fn({ type: "push_notification", channel: "notifications_changed", payload });
  };
  return { client: client as never, calls, push, setRows: (next: FloorNotification[]) => (rows = next) };
}

describe("what it lists", () => {
  it("asks the floor, not an extension", async () => {
    const { client, calls } = backendWith([row()]);

    render(<NotificationsPanel client={client} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText("Runbook")).toBeTruthy());
    expect(calls.map((c) => c.method)).toEqual(["notifications_list"]);
  });

  it("shows both paths with the extension each is from", async () => {
    const { client } = backendWith([
      row(),
      row({ id: "local-1", origin: "local", source: "acme.scanner", title: "Export ready", actor: "" }),
    ]);

    render(
      <NotificationsPanel
        client={client}
        sourceName={(id) => ({ "igni.artifacts": "Artifacts", "acme.scanner": "Scanner" })[id] ?? id}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByText("Export ready")).toBeTruthy());
    expect(screen.getByText("Artifacts")).toBeTruthy();
    expect(screen.getByText("Scanner")).toBeTruthy();
    expect(screen.getByText("2 unread")).toBeTruthy();
  });

  it("keeps what it has and says why when igni server could not be asked", async () => {
    const { client } = backendWith([row()]);
    (client as unknown as { rpc: ReturnType<typeof vi.fn> }).rpc.mockResolvedValueOnce({
      notifications: [row()],
      counts: { unread: 1, by_source: { "igni.artifacts": 1 } },
      server_error: "igni could not be reached",
    });

    render(<NotificationsPanel client={client} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText(/could not be reached/)).toBeTruthy());
    expect(screen.getByText("Runbook")).toBeTruthy();
  });

  it("reads again when the counts move", async () => {
    const { client, calls, push, setRows } = backendWith([row()]);
    render(<NotificationsPanel client={client} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Runbook")).toBeTruthy());

    setRows([row(), row({ id: "s2", title: "Onboarding" })]);
    act(() => push({ counts: { unread: 2, by_source: { "igni.artifacts": 2 } } }));

    await waitFor(() => expect(screen.getByText("Onboarding")).toBeTruthy());
    expect(calls.filter((c) => c.method === "notifications_list").length).toBe(2);
  });

  it("does not read again for some other push", async () => {
    const { client, calls } = backendWith([row()]);
    render(<NotificationsPanel client={client} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Runbook")).toBeTruthy());

    expect(calls.length).toBe(1);
  });
});

describe("opening one", () => {
  it("goes to its page with its params, marks it read, and closes", async () => {
    const { client, calls } = backendWith([row()]);
    const opened = vi.fn();
    const closed = vi.fn();
    const told: NotificationCounts[] = [];

    render(
      <NotificationsPanel
        client={client}
        canOpen={(page) => page === "igni.artifacts"}
        onOpenTarget={opened}
        onCountsChanged={(c) => told.push(c)}
        onClose={closed}
      />,
    );
    await waitFor(() => expect(screen.getByText("Runbook")).toBeTruthy());

    fireEvent.click(screen.getByTitle(/Open in/));

    expect(opened).toHaveBeenCalledWith(
      { page: "igni.artifacts", params: { slug: "runbook", comment: "c1" } },
      expect.objectContaining({ id: "s1" }),
    );
    expect(closed).toHaveBeenCalled();
    await waitFor(() =>
      expect(calls).toContainEqual({ method: "notifications_mark_read", args: { ids: ["s1"] } }),
    );
    await waitFor(() => expect(told.at(-1)?.unread).toBe(0));
  });

  it("does not mark again what was already read", async () => {
    const { client, calls } = backendWith([row({ read: true })]);
    render(<NotificationsPanel client={client} onOpenTarget={() => {}} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Runbook")).toBeTruthy());

    fireEvent.click(screen.getByTitle(/Open in/));

    expect(calls.some((c) => c.method === "notifications_mark_read")).toBe(false);
  });

  it("cannot open one whose page this app does not have", async () => {
    const { client } = backendWith([row()]);
    const opened = vi.fn();
    render(
      <NotificationsPanel client={client} canOpen={() => false} onOpenTarget={opened} onClose={() => {}} />,
    );
    await waitFor(() => expect(screen.getByText("Runbook")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /Runbook/ }));

    expect(opened).not.toHaveBeenCalled();
  });
});

describe("marking everything read", () => {
  it("empties the count every badge reads", async () => {
    const { client, calls } = backendWith([row(), row({ id: "local-1", origin: "local" })]);
    const told: NotificationCounts[] = [];
    render(<NotificationsPanel client={client} onCountsChanged={(c) => told.push(c)} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("2 unread")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /mark all read/i }));

    await waitFor(() => expect(screen.getByText("nothing unread")).toBeTruthy());
    expect(calls).toContainEqual({ method: "notifications_mark_read", args: {} });
    expect(told.at(-1)).toEqual({ unread: 0, by_source: {} });
  });

  it("offers nothing to mark when nothing is unread", async () => {
    const { client } = backendWith([]);
    render(<NotificationsPanel client={client} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText("nothing unread")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /mark all read/i })).toBeNull();
  });

  it("does not report a number it failed to read", async () => {
    // "Nothing is waiting" is the one answer this must not give
    // wrongly, and a failed read is not evidence of an empty queue.
    const told: NotificationCounts[] = [];
    const client = {
      rpc: vi.fn(async () => ({ error: "Could not reach the backend" })),
      onEvent: () => () => {},
    } as never;

    render(<NotificationsPanel client={client} onCountsChanged={(c) => told.push(c)} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText(/Could not reach the backend/)).toBeTruthy());
    expect(told).toEqual([]);
  });
});
