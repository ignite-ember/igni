// @vitest-environment jsdom
/**
 * The watcher must be able to correct itself.
 *
 * The panel learns about exits from the ``process_exited`` push. If the
 * backend that would have sent it is killed, that push never arrives —
 * and the seed RPC could not fix it either, because it skipped any pid
 * the panel already held (``if (!next.has(p.pid))``). A window left
 * open across a BE restart therefore showed dead processes as running,
 * ticking their elapsed time, offering a Kill button for something that
 * had exited minutes earlier.
 *
 * The backend is authoritative about process state. These tests pin
 * that the panel takes its answer.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { WatcherPanel } from "./WatcherPanel";

afterEach(cleanup);

type Listener = (m: unknown) => void;

/** A client whose list RPC returns ``rows`` and whose push channel we
 *  can drive by hand. */
function fakeClient(rows: unknown[]) {
  let listener: Listener = () => {};
  return {
    client: {
      rpc: vi.fn(async (method: string) => {
        if (method === "list_background_processes") return rows;
        if (method === "read_process_tail") {
          return { pid: 0, output: "", is_running: false, exit_code: null };
        }
        return null;
      }),
      onEvent: (fn: Listener) => {
        listener = fn;
        return () => {};
      },
    } as never,
    push: (m: unknown) => listener(m),
  };
}

function started(pid: number, cmd: string) {
  return {
    type: "push_notification",
    channel: "process_started",
    payload: { pid, cmd, started_at: Date.now() / 1000 },
  };
}

describe("a row the backend says is finished", () => {
  it("stops being shown as running, even though it was pushed as started", async () => {
    // The BE's list reports it finished — this is what a panel sees
    // after the restart that swallowed the exit push.
    const { client, push } = fakeClient([
      { pid: 4242, cmd: "npm run build", elapsed_seconds: 30, is_running: false, exit_code: 1 },
    ]);
    render(<WatcherPanel client={client} onClose={() => {}} />);

    // Arrives first, marked running, exactly as the old BE said.
    push(started(4242, "npm run build"));
    await waitFor(() => expect(screen.getByText(/PID 4242/)).toBeTruthy());

    // The seed RPC has the truth; the row must take it.
    await waitFor(() => expect(screen.getByText("exit 1")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /kill/i })).toBeNull();
  });

  it("is not offered a kill button once it is known to be dead", async () => {
    const { client } = fakeClient([
      { pid: 77, cmd: "sleep 1", elapsed_seconds: 2, is_running: false, exit_code: 0 },
    ]);
    render(<WatcherPanel client={client} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText("exit 0")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /kill/i })).toBeNull();
  });
});

describe("a row the backend has never heard of", () => {
  it("keeps running rows the snapshot cannot speak for", async () => {
    // The reconciliation is deliberately narrow: it only corrects pids
    // the panel already held when it asked. A row that arrives from a
    // push afterwards is newer than the answer, and marking it stopped
    // on the strength of a snapshot taken before it existed would be
    // the same class of mistake in the other direction.
    //
    // The cost is a ghost from a dead BE that the panel learns about
    // only via a late push — it stays "running" until the panel is
    // reopened, at which point the seed does know about it and
    // reconciles. Pinned here so the trade-off is deliberate rather
    // than discovered.
    const { client, push } = fakeClient([]);
    render(<WatcherPanel client={client} onClose={() => {}} />);

    push(started(999, "ghost from a previous life"));
    await waitFor(() => expect(screen.getByText(/PID 999/)).toBeTruthy());

    expect(screen.queryByRole("button", { name: /kill/i })).not.toBeNull();
  });
});

describe("a process that starts while the seed is in flight", () => {
  it("survives the reconciliation", async () => {
    // The snapshot was taken before it existed, so its absence from
    // the list says nothing about it.
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((r) => (release = r));
    let listener: Listener = () => {};
    const client = {
      rpc: vi.fn(async (method: string) => {
        if (method === "list_background_processes") {
          await gate;
          return [];
        }
        return null;
      }),
      onEvent: (fn: Listener) => {
        listener = fn;
        return () => {};
      },
    } as never;

    render(<WatcherPanel client={client} onClose={() => {}} />);
    listener(started(555, "started mid-fetch"));
    await waitFor(() => expect(screen.getByText(/PID 555/)).toBeTruthy());

    release(null);

    // Still running: the empty snapshot predates it.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /kill/i })).not.toBeNull(),
    );
  });
});
