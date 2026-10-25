// @vitest-environment jsdom
/**
 * `bridge.service`: the page's own service, reached directly.
 *
 * The host hands the page a token for this extension's service and no
 * other; the page keeps it until shortly before it expires, attaches
 * it to `fetch`, and sends it as the first frame of a WebSocket — which
 * is how a browser socket says who it is, since it cannot set a header.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { connect } from "./bridge";
import { BRIDGE_VERSION } from "./protocol";
import { RENEW_BEFORE_MS, ServiceError, ServiceLink } from "./service";

const SERVICE = "https://notes.example.com/api";

/** A WebSocket that opens when told to, and records what it was sent. */
class FakeSocket extends EventTarget {
  static made: FakeSocket[] = [];
  sent: string[] = [];
  constructor(
    readonly url: string,
    readonly protocols?: string | string[],
  ) {
    super();
    FakeSocket.made.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  open() {
    this.dispatchEvent(new Event("open"));
  }
  refuse() {
    this.dispatchEvent(new Event("close"));
  }
}

afterEach(() => {
  FakeSocket.made = [];
  Object.defineProperty(window, "parent", { value: window, configurable: true });
});

function issuing(...tokens: string[]) {
  const asked = vi.fn(async () => ({ token: tokens.shift() ?? "spent", expiresIn: 600 }));
  return asked;
}

describe("the token", () => {
  it("is fetched once and kept while it is fresh", async () => {
    let now = 0;
    const ask = issuing("t1", "t2");
    const link = new ServiceLink(SERVICE, ask, { now: () => now });

    expect(await link.token()).toBe("t1");
    now += 300_000;
    expect(await link.token()).toBe("t1");
    expect(ask).toHaveBeenCalledTimes(1);
  });

  it("is renewed in its last minute", async () => {
    let now = 0;
    const ask = issuing("t1", "t2");
    const link = new ServiceLink(SERVICE, ask, { now: () => now });

    await link.token();
    now += 600_000 - RENEW_BEFORE_MS + 1;
    expect(await link.token()).toBe("t2");
  });

  it("is one request however many ask at once", async () => {
    const ask = issuing("t1");
    const link = new ServiceLink(SERVICE, ask);
    const all = await Promise.all([link.token(), link.token(), link.token()]);
    expect(all).toEqual(["t1", "t1", "t1"]);
    expect(ask).toHaveBeenCalledTimes(1);
  });

  it("is refused outright when the extension has no service", async () => {
    const ask = issuing("t1");
    const link = new ServiceLink(null, ask);
    await expect(link.token()).rejects.toMatchObject({ code: "no_service" });
    expect(ask).not.toHaveBeenCalled();
  });
});

describe("fetch", () => {
  it("goes to a path on the service with the token", async () => {
    const seen: { url: string; auth: string | null; credentials?: RequestCredentials }[] = [];
    const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      seen.push({
        url: String(url),
        auth: new Headers(init?.headers).get("Authorization"),
        credentials: init?.credentials,
      });
      return new Response("{}", { status: 200 });
    });
    const link = new ServiceLink(SERVICE, issuing("t1"), { fetch: fetcher as typeof fetch });

    await link.fetch("/v1/notes?x=1");

    expect(seen).toEqual([
      {
        url: "https://notes.example.com/api/v1/notes?x=1",
        auth: "Bearer t1",
        credentials: "omit",
      },
    ]);
  });

  it("never sends cookies, even when asked to", async () => {
    let credentials: RequestCredentials | undefined;
    const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      credentials = init?.credentials;
      return new Response("{}", { status: 200 });
    });
    const link = new ServiceLink(SERVICE, issuing("t1"), { fetch: fetcher as typeof fetch });

    await link.fetch("/v1/notes", { credentials: "include" });

    expect(credentials).toBe("omit");
  });

  it("fetches a new token once after a 401", async () => {
    const auths: (string | null)[] = [];
    const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      auths.push(new Headers(init?.headers).get("Authorization"));
      return new Response("", { status: auths.length === 1 ? 401 : 200 });
    });
    const link = new ServiceLink(SERVICE, issuing("old", "new"), {
      fetch: fetcher as typeof fetch,
    });

    const response = await link.fetch("v1/notes");

    expect(response.status).toBe(200);
    expect(auths).toEqual(["Bearer old", "Bearer new"]);
  });

  it.each([
    "https://elsewhere.example/steal",
    "//elsewhere.example/steal",
    "javascript:alert(1)",
  ])("never sends the token anywhere but the service: %s", async (path) => {
    const fetcher = vi.fn();
    const link = new ServiceLink(SERVICE, issuing("t1"), { fetch: fetcher as typeof fetch });
    await expect(link.fetch(path)).rejects.toMatchObject({ code: "other_origin" });
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("websocket", () => {
  it("opens wss on the service and says who it is first", async () => {
    const link = new ServiceLink(SERVICE, issuing("t1"), {
      WebSocket: FakeSocket as unknown as typeof WebSocket,
    });

    const opening = link.websocket("/v1/artifacts/shared/doc/room");
    await vi.waitFor(() => expect(FakeSocket.made).toHaveLength(1));
    const socket = FakeSocket.made[0];
    expect(socket.url).toBe("wss://notes.example.com/api/v1/artifacts/shared/doc/room");
    expect(socket.sent).toEqual([]);

    socket.open();

    expect(await opening).toBe(socket);
    expect(socket.sent.map((frame) => JSON.parse(frame))).toEqual([
      { type: "auth", token: "t1" },
    ]);
  });

  it("is ws for a plain-http service on this machine", async () => {
    const link = new ServiceLink("http://localhost:8000", issuing("t1"), {
      WebSocket: FakeSocket as unknown as typeof WebSocket,
    });
    void link.websocket("room");
    await vi.waitFor(() => expect(FakeSocket.made).toHaveLength(1));
    expect(FakeSocket.made[0].url).toBe("ws://localhost:8000/room");
  });

  it("takes another first frame when the service wants one", async () => {
    const link = new ServiceLink(SERVICE, issuing("t1"), {
      WebSocket: FakeSocket as unknown as typeof WebSocket,
    });
    const opening = link.websocket("room", { authFrame: (t) => `TOKEN ${t}` });
    await vi.waitFor(() => expect(FakeSocket.made).toHaveLength(1));
    FakeSocket.made[0].open();
    await opening;
    expect(FakeSocket.made[0].sent).toEqual(["TOKEN t1"]);
  });

  it("rejects when the socket closes before it opens", async () => {
    const link = new ServiceLink(SERVICE, issuing("t1"), {
      WebSocket: FakeSocket as unknown as typeof WebSocket,
    });
    const opening = link.websocket("room");
    await vi.waitFor(() => expect(FakeSocket.made).toHaveLength(1));
    FakeSocket.made[0].refuse();
    await expect(opening).rejects.toBeInstanceOf(ServiceError);
  });
});

describe("through the bridge", () => {
  function frameWith(serviceUrl: string | null | undefined) {
    const posted: Record<string, unknown>[] = [];
    const host = {
      postMessage: (message: Record<string, unknown>) => posted.push(message),
    } as unknown as Window;
    Object.defineProperty(window, "parent", { value: host, configurable: true });
    const send = (body: Record<string, unknown>) => {
      const event = new MessageEvent("message", { data: { igni: BRIDGE_VERSION, ...body } });
      Object.defineProperty(event, "source", { value: host });
      window.dispatchEvent(event);
    };
    const pending = connect();
    send({
      kind: "hello",
      nonce: "n",
      extensionId: "acme.notes",
      params: {},
      theme: "dark",
      tokens: {},
      ...(serviceUrl === undefined ? {} : { serviceUrl }),
    });
    return { posted, send, pending };
  }

  it("names the service the host said, and asks the host for its token", async () => {
    const { posted, send, pending } = frameWith(SERVICE);
    const bridge = await pending;
    expect(bridge.service.url).toBe(SERVICE);

    const token = bridge.service.token();
    const asked = posted.find((m) => m.kind === "service-token");
    // No audience on the wire: the host decides whose service it is.
    expect(asked).toEqual({ igni: BRIDGE_VERSION, kind: "service-token", nonce: "n", id: asked?.id });

    send({ kind: "service-token-reply", id: asked?.id, ok: true, token: "t1", expiresIn: 600 });
    await expect(token).resolves.toBe("t1");
  });

  it("rejects with the host's sentence when there is no token", async () => {
    const { posted, send, pending } = frameWith(SERVICE);
    const bridge = await pending;
    const token = bridge.service.token();
    const asked = posted.find((m) => m.kind === "service-token");
    send({ kind: "service-token-reply", id: asked?.id, ok: false, error: "nobody is signed in to igni" });
    await expect(token).rejects.toThrow(/signed in/);
  });

  it("has no service when the host named none — an older host included", async () => {
    const bridge = await frameWith(undefined).pending;
    expect(bridge.service.url).toBeNull();
    await expect(bridge.service.token()).rejects.toMatchObject({ code: "no_service" });
  });
});
