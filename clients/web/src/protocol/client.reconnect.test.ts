/**
 * Reconnect behaviour: backoff, the "replaced" yield, and teardown.
 *
 * ``client.test.ts`` says the connection lifecycle is "too entangled with
 * the real Web API to unit-test usefully — covered by Playwright e2e".
 * Checked before contradicting it, and it is half true:
 *
 *   * ``e2e/app.spec.ts`` does cover "server crash → composer flips to
 *     'Connecting…'", so the basic disconnect flip is exercised end to end.
 *   * Nothing covers the backoff schedule, its reset, ``close()`` during a
 *     pending retry, or close code 1008 — and the e2e comment excludes that
 *     last one explicitly, as "BE-initiated and platform-specific".
 *
 * Those are the parts that break quietly. A laptop waking from sleep, a
 * network change, a BE restart: all land here, and a wrong backoff or a
 * socket that resurrects itself is invisible until a user is sitting in
 * front of it.
 *
 * A stubbed ``WebSocket`` plus fake timers covers this fine, which is the
 * other half of the answer: the lifecycle is unit-testable after all.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** Minimal stand-in for the browser WebSocket, recording every instance. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closeCalls = 0;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  close(): void {
    this.closeCalls += 1;
  }

  send(_data: string): void {
    /* not exercised here */
  }

  /** Drive the socket the way a browser would. */
  open(): void {
    this.onopen?.();
  }
  fail(code = 1006): void {
    this.onclose?.({ code });
  }
}

let IgniClient: typeof import("./client").IgniClient;

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  FakeWebSocket.instances = [];
  (globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket;
  (globalThis as { window?: unknown }).window = { location: { search: "" } };
  (globalThis as { document?: unknown }).document = { querySelector: () => null };
  vi.spyOn(console, "info").mockImplementation(() => undefined);
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  ({ IgniClient } = await import("./client"));
});

afterEach(() => {
  vi.useRealTimers();
  delete (globalThis as { WebSocket?: unknown }).WebSocket;
  delete (globalThis as { window?: unknown }).window;
  delete (globalThis as { document?: unknown }).document;
  vi.restoreAllMocks();
});

const latest = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];

describe("backoff schedule", () => {
  it("retries after 500ms on an abnormal close", () => {
    const client = new IgniClient("ws://test");
    client.connect();
    expect(FakeWebSocket.instances).toHaveLength(1);

    latest().fail();

    // Not yet — the retry is scheduled, not immediate.
    vi.advanceTimersByTime(499);
    expect(FakeWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("doubles up to a 5s ceiling", () => {
    const client = new IgniClient("ws://test");
    client.connect();

    // Each failure without an intervening open should wait longer, and
    // stop growing at 5s. Asserted as "the socket appears only after the
    // expected delay", which is the observable behaviour.
    for (const expected of [500, 1000, 2000, 4000, 5000, 5000]) {
      const before = FakeWebSocket.instances.length;
      latest().fail();
      vi.advanceTimersByTime(expected - 1);
      expect(FakeWebSocket.instances).toHaveLength(before);
      vi.advanceTimersByTime(1);
      expect(FakeWebSocket.instances).toHaveLength(before + 1);
    }
  });

  it("resets to 500ms after a successful open", () => {
    const client = new IgniClient("ws://test");
    client.connect();

    // Walk the backoff up first.
    latest().fail();
    vi.advanceTimersByTime(500);
    latest().fail();
    vi.advanceTimersByTime(1000);

    // A good connection clears the penalty…
    latest().open();

    // …so the next failure waits 500ms again, not 2000ms.
    const before = FakeWebSocket.instances.length;
    latest().fail();
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances).toHaveLength(before + 1);
  });
});

describe("close code 1008 — another tab took the slot", () => {
  it("yields instead of reconnecting", () => {
    const states: string[] = [];
    const client = new IgniClient("ws://test");
    client.onStateChange((s) => states.push(s));
    client.connect();

    latest().fail(1008);

    expect(states).toContain("replaced");
    // Auto-reconnecting here would have the two tabs displace each other
    // forever, which is why the code deliberately stops.
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("does not emit 'disconnected' for the replaced case", () => {
    const states: string[] = [];
    const client = new IgniClient("ws://test");
    client.onStateChange((s) => states.push(s));
    client.connect();

    latest().fail(1008);

    expect(states).not.toContain("disconnected");
  });
});

describe("teardown", () => {
  it("close() stops a retry that was already scheduled", () => {
    const client = new IgniClient("ws://test");
    client.connect();

    latest().fail(); // schedules a retry
    client.close(); // caller is done with this client

    vi.advanceTimersByTime(60_000);

    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("a socket replaced by connect() cannot flip the shared state", () => {
    const states: string[] = [];
    const client = new IgniClient("ws://test");
    client.onStateChange((s) => states.push(s));
    client.connect();
    const orphan = latest();

    client.connect(); // replaces it
    states.length = 0;

    // The orphan's handlers were nulled on replacement, so driving it is a
    // no-op. If they leaked, a dead socket could report "connected".
    orphan.onopen?.();
    orphan.onclose?.({ code: 1006 });

    expect(states).toHaveLength(0);
  });
});
