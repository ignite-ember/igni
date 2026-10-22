// @vitest-environment jsdom
/**
 * The panel, and the number outside it.
 *
 * The bell keeps its own count, fed by what the server pushes. The
 * server pushes when somebody else does something — and pushes
 * nothing when *you* deal with something, because it has nothing to
 * tell you that you did not just do. That left "Mark all seen"
 * emptying the panel and leaving the bell reading six, with nothing
 * behind it: the one number in the app whose whole job is to be
 * believed, quietly wrong until the next reload.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { NotificationsPanel } from "./NotificationsPanel";

afterEach(cleanup);

function row(over: Record<string, unknown> = {}) {
  return {
    id: "n1",
    kind: "comment",
    actor_email: "sam@acme.test",
    slug: "abc123",
    title: "Release checklist",
    body: "Does this still hold?",
    created_at: new Date().toISOString(),
    read: false,
    ...over,
  };
}

/** A client whose notification count drops to zero once marked. */
function clientWith(rows: unknown[]) {
  let waiting = rows.length;
  let left = [...rows];
  const calls: string[] = [];
  return {
    calls,
    client: {
      rpc: vi.fn(async (method: string) => {
        calls.push(method);
        if (method === "artifact_mark_seen") {
          waiting = 0;
          left = [];
          return { ok: true, waiting };
        }
        return { ok: true, notifications: left, waiting };
      }),
      onEvent: () => () => {},
    } as never,
  };
}

describe("the count the bell shows", () => {
  it("is reported as soon as the panel reads it", async () => {
    const told: number[] = [];
    const { client } = clientWith([row(), row({ id: "n2" })]);

    render(
      <NotificationsPanel
        client={client}
        onWaitingChanged={(n) => told.push(n)}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(told).toContain(2));
  });

  it("drops to zero when everything is marked seen", async () => {
    // The defect: the panel emptied and the bell did not.
    const told: number[] = [];
    const { client } = clientWith([row()]);

    render(
      <NotificationsPanel
        client={client}
        onWaitingChanged={(n) => told.push(n)}
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(screen.getByText(/1 waiting/)).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /mark all seen/i }));

    await waitFor(() => expect(told.at(-1)).toBe(0));
  });

  it("does not report a number it failed to read", async () => {
    // "Nothing is waiting" is the one answer this must not give
    // wrongly, and a failed read is not evidence of an empty queue.
    const told: number[] = [];
    const client = {
      rpc: vi.fn(async () => ({
        error: "Could not reach the server: ReadError",
      })),
      onEvent: () => () => {},
    } as never;

    render(
      <NotificationsPanel
        client={client}
        onWaitingChanged={(n) => told.push(n)}
        onClose={() => {}}
      />,
    );

    await waitFor(() =>
      expect(screen.getByText(/Could not reach the server/)).toBeTruthy(),
    );
    expect(told).toEqual([]);
  });
});

describe("what it shows", () => {
  it("lists what is waiting", async () => {
    const { client } = clientWith([row()]);

    render(<NotificationsPanel client={client} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText(/commented on/)).toBeTruthy());
  });

  it("offers nothing to mark when nothing is waiting", async () => {
    const { client } = clientWith([]);

    render(<NotificationsPanel client={client} onClose={() => {}} />);

    await waitFor(() =>
      expect(screen.getByText("nothing waiting")).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: /mark all seen/i })).toBeNull();
  });

  it("works without anybody listening for the count", async () => {
    // The callback is optional; the panel is usable on its own.
    const { client } = clientWith([row()]);

    render(<NotificationsPanel client={client} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/1 waiting/)).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /mark all seen/i }));

    await waitFor(() =>
      expect(screen.getByText("nothing waiting")).toBeTruthy(),
    );
  });
});
