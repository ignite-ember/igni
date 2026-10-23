// @vitest-environment jsdom
/**
 * An extension that can be linked to.
 *
 * `params` arrived exactly once, inside `hello`, and there was no
 * message that could carry new ones. Combined with the frame being
 * unable to write the address bar (`allow-top-navigation` is
 * deliberately absent) and its `localStorage` dying with the
 * ephemeral port it is served from, an extension's internal location
 * was unaddressable, unsavable and unrestorable.
 *
 * That is the difference between a widget and a page, and the
 * reference extension could never have surfaced it: a compliance
 * table has one view.
 *
 * Two additive envelope kinds fix it — additive is never breaking
 * under §8 — and these are the behaviours that make them worth
 * having rather than just present.
 */
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ExtensionFrame } from "./ExtensionFrame";
import { BRIDGE_VERSION, isGuestMessage, isHostMessage } from "./protocol";

afterEach(cleanup);

// ── The envelopes ──────────────────────────────────────────────────

describe("the params message", () => {
  it("is accepted by the guest", () => {
    expect(
      isHostMessage({
        igni: BRIDGE_VERSION,
        kind: "params",
        params: { id: "7" },
      }),
    ).toBe(true);
  });

  it("is refused without a params object", () => {
    expect(isHostMessage({ igni: BRIDGE_VERSION, kind: "params" })).toBe(false);
    expect(
      isHostMessage({ igni: BRIDGE_VERSION, kind: "params", params: [] }),
    ).toBe(false);
  });
});

describe("the location message", () => {
  const nonce = "n1";

  it("is accepted when every value is a string", () => {
    expect(
      isGuestMessage({
        igni: BRIDGE_VERSION,
        kind: "location",
        nonce,
        state: { tab: "columns", table: "orders" },
      }),
    ).toBe(true);
  });

  it("is refused when a value is not a string", () => {
    // This ends up in the host's address bar and the guest is the
    // untrusted party, so the shape is checked rather than assumed.
    expect(
      isGuestMessage({
        igni: BRIDGE_VERSION,
        kind: "location",
        nonce,
        state: { depth: 3 },
      }),
    ).toBe(false);
  });

  it("is refused when state is an array", () => {
    expect(
      isGuestMessage({
        igni: BRIDGE_VERSION,
        kind: "location",
        nonce,
        state: ["a"],
      }),
    ).toBe(false);
  });

  it("is refused without a nonce, like every other guest message", () => {
    expect(
      isGuestMessage({ igni: BRIDGE_VERSION, kind: "location", state: {} }),
    ).toBe(false);
  });

  it("accepts empty state, which is how a page says 'nowhere in particular'", () => {
    expect(
      isGuestMessage({
        igni: BRIDGE_VERSION,
        kind: "location",
        nonce,
        state: {},
      }),
    ).toBe(true);
  });
});

// ── The host half ──────────────────────────────────────────────────

/**
 * Mount a frame and capture what it posts into it.
 *
 * jsdom gives an iframe a real `contentWindow`, so the component's
 * `post` works; this replaces `postMessage` on it to record.
 */
function mountFrame(props: Partial<Parameters<typeof ExtensionFrame>[0]> = {}) {
  const posted: unknown[] = [];
  const result = render(
    <ExtensionFrame
      extensionId="acme.scanner"
      origin="http://127.0.0.1:41234"
      allow={["ext.acme.scanner.*"]}
      onCall={() => Promise.resolve(null)}
      {...props}
    />,
  );
  const frame = result.container.querySelector("iframe");
  if (frame?.contentWindow) {
    vi.spyOn(frame.contentWindow, "postMessage").mockImplementation(
      (message: unknown) => {
        posted.push(message);
      },
    );
  }
  return { ...result, frame, posted };
}

type Posted = { kind: string; params?: Record<string, unknown> };

describe("params under a mounted frame", () => {
  it("sends nothing before the frame has loaded", () => {
    // `hello` carries the initial value, so posting `params` as well
    // would deliver it twice and fire `onParams` for something that
    // never changed.
    const { posted, rerender } = mountFrame({ params: { id: "1" } });
    rerender(
      <ExtensionFrame
        extensionId="acme.scanner"
        origin="http://127.0.0.1:41234"
        allow={[]}
        onCall={() => Promise.resolve(null)}
        params={{ id: "2" }}
      />,
    );
    expect(
      (posted as Posted[]).filter((m) => m.kind === "params"),
    ).toHaveLength(0);
  });

  it("sends them when they change after the handshake", async () => {
    const { posted, frame, rerender } = mountFrame({ params: { id: "1" } });
    frame?.dispatchEvent(new Event("load"));
    await waitFor(() =>
      expect((posted as Posted[]).some((m) => m.kind === "hello")).toBe(true),
    );

    rerender(
      <ExtensionFrame
        extensionId="acme.scanner"
        origin="http://127.0.0.1:41234"
        allow={[]}
        onCall={() => Promise.resolve(null)}
        params={{ id: "2" }}
      />,
    );

    await waitFor(() => {
      const sent = (posted as Posted[]).filter((m) => m.kind === "params");
      expect(sent).toHaveLength(1);
      expect(sent[0].params).toEqual({ id: "2" });
    });
  });

  it("does not resend an equal object from a parent re-render", async () => {
    // The common caller writes `params={{ id }}`, a fresh object
    // every render. Comparing by identity would post a message on
    // every parent update and the frame would react to each one.
    const { posted, frame, rerender } = mountFrame({ params: { id: "1" } });
    frame?.dispatchEvent(new Event("load"));
    await waitFor(() =>
      expect((posted as Posted[]).some((m) => m.kind === "hello")).toBe(true),
    );

    for (let i = 0; i < 3; i++) {
      rerender(
        <ExtensionFrame
          extensionId="acme.scanner"
          origin="http://127.0.0.1:41234"
          allow={[]}
          onCall={() => Promise.resolve(null)}
          params={{ id: "1" }}
        />,
      );
    }

    expect(
      (posted as Posted[]).filter((m) => m.kind === "params"),
    ).toHaveLength(0);
  });
});

describe("location from the frame", () => {
  it("reaches onLocation, and is not a navigation", () => {
    const onLocation = vi.fn();
    const onNavigate = vi.fn();
    const { frame } = mountFrame({ onLocation, onNavigate });

    // Replay what the guest posts, with the nonce the frame issued.
    const hello = frame?.contentWindow?.postMessage as unknown as {
      mock: { calls: unknown[][] };
    };
    frame?.dispatchEvent(new Event("load"));
    const sent = hello.mock.calls[0][0] as { nonce: string };

    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          igni: BRIDGE_VERSION,
          kind: "location",
          nonce: sent.nonce,
          state: { tab: "columns" },
        },
        origin: "http://127.0.0.1:41234",
        source: frame?.contentWindow,
      }),
    );

    expect(onLocation).toHaveBeenCalledWith({ tab: "columns" });
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("is dropped when the nonce is wrong, like every other guest message", () => {
    const onLocation = vi.fn();
    const { frame } = mountFrame({ onLocation });
    frame?.dispatchEvent(new Event("load"));

    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          igni: BRIDGE_VERSION,
          kind: "location",
          nonce: "not-the-one",
          state: { tab: "columns" },
        },
        origin: "http://127.0.0.1:41234",
        source: frame?.contentWindow,
      }),
    );

    expect(onLocation).not.toHaveBeenCalled();
  });

  it("is safe to ignore — a host with no handler just is not addressable", () => {
    const { frame } = mountFrame();
    frame?.dispatchEvent(new Event("load"));
    expect(() =>
      window.dispatchEvent(
        new MessageEvent("message", {
          data: {
            igni: BRIDGE_VERSION,
            kind: "location",
            nonce: "whatever",
            state: {},
          },
          origin: "http://127.0.0.1:41234",
          source: frame?.contentWindow,
        }),
      ),
    ).not.toThrow();
  });
});

// ── Events ─────────────────────────────────────────────────────────

describe("the event message", () => {
  it("is accepted with a topic and a payload", () => {
    expect(
      isHostMessage({
        igni: BRIDGE_VERSION,
        kind: "event",
        topic: "workspace.head-changed",
        payload: { commit_sha: "abc" },
      }),
    ).toBe(true);
  });

  it("is refused without a topic", () => {
    expect(
      isHostMessage({ igni: BRIDGE_VERSION, kind: "event", payload: {} }),
    ).toBe(false);
  });

  it("is refused when the payload is not an object", () => {
    expect(
      isHostMessage({
        igni: BRIDGE_VERSION,
        kind: "event",
        topic: "a.b",
        payload: "nope",
      }),
    ).toBe(false);
  });
});

describe("events reaching a frame", () => {
  it("posts one in, once the frame has been greeted", async () => {
    const { posted, frame, rerender } = mountFrame();
    frame?.dispatchEvent(new Event("load"));
    await waitFor(() =>
      expect((posted as Posted[]).some((m) => m.kind === "hello")).toBe(true),
    );

    rerender(
      <ExtensionFrame
        extensionId="acme.scanner"
        origin="http://127.0.0.1:41234"
        allow={[]}
        onCall={() => Promise.resolve(null)}
        events={[
          {
            topic: "workspace.head-changed",
            payload: { commit_sha: "a" },
            n: 1,
          },
        ]}
      />,
    );

    await waitFor(() => {
      const sent = (posted as { kind: string; topic?: string }[]).filter(
        (m) => m.kind === "event",
      );
      expect(sent).toHaveLength(1);
      expect(sent[0].topic).toBe("workspace.head-changed");
    });
  });

  it("sends nothing before the frame has been greeted", () => {
    // A frame with no nonce has no listener, and an event posted
    // into it is one the guest never sees.
    const { posted } = mountFrame({
      events: [{ topic: "workspace.head-changed", payload: {}, n: 1 }],
    });
    expect((posted as Posted[]).filter((m) => m.kind === "event")).toHaveLength(
      0,
    );
  });

  it("delivers the same event twice when it happens twice", async () => {
    // Counted rather than compared: a guest waiting for "the file
    // changed again" has to hear it again.
    const { posted, frame, rerender } = mountFrame();
    frame?.dispatchEvent(new Event("load"));
    await waitFor(() =>
      expect((posted as Posted[]).some((m) => m.kind === "hello")).toBe(true),
    );

    const send = (n: number) =>
      rerender(
        <ExtensionFrame
          extensionId="acme.scanner"
          origin="http://127.0.0.1:41234"
          allow={[]}
          onCall={() => Promise.resolve(null)}
          events={[
            { topic: "workspace.files-changed", payload: { paths: [] }, n },
          ]}
        />,
      );

    send(1);
    send(2);
    await waitFor(() =>
      expect(
        (posted as Posted[]).filter((m) => m.kind === "event"),
      ).toHaveLength(2),
    );
  });

  it("delivers both when two arrive in one render", async () => {
    // React batches, so two pushes that land in the same tick are
    // one re-render. A single-slot prop would show only the second
    // and the first would be gone — which a commit makes real,
    // because it moves HEAD and changes the dirty set at once.
    const { posted, frame, rerender } = mountFrame();
    frame?.dispatchEvent(new Event("load"));
    await waitFor(() =>
      expect((posted as Posted[]).some((m) => m.kind === "hello")).toBe(true),
    );

    rerender(
      <ExtensionFrame
        extensionId="acme.scanner"
        origin="http://127.0.0.1:41234"
        allow={[]}
        onCall={() => Promise.resolve(null)}
        events={[
          { topic: "workspace.files-changed", payload: { paths: ["a"] }, n: 1 },
          {
            topic: "workspace.head-changed",
            payload: { commit_sha: "b" },
            n: 2,
          },
        ]}
      />,
    );

    await waitFor(() => {
      const sent = (posted as { kind: string; topic?: string }[]).filter(
        (m) => m.kind === "event",
      );
      expect(sent.map((m) => m.topic)).toEqual([
        "workspace.files-changed",
        "workspace.head-changed",
      ]);
    });
  });

  it("does not replay the backlog to a frame that just opened", async () => {
    // A panel the user closed and reopened is not owed what it
    // missed. It is about to read the current state itself, and
    // three-minute-old events arriving as news is the same mistake
    // the host's watcher avoids by never announcing its first
    // observation.
    const { posted, frame, rerender } = mountFrame({
      events: [
        { topic: "workspace.head-changed", payload: { commit_sha: "a" }, n: 1 },
        { topic: "workspace.head-changed", payload: { commit_sha: "b" }, n: 2 },
      ],
    });
    frame?.dispatchEvent(new Event("load"));
    await waitFor(() =>
      expect((posted as Posted[]).some((m) => m.kind === "hello")).toBe(true),
    );
    expect((posted as Posted[]).filter((m) => m.kind === "event")).toHaveLength(
      0,
    );

    rerender(
      <ExtensionFrame
        extensionId="acme.scanner"
        origin="http://127.0.0.1:41234"
        allow={[]}
        onCall={() => Promise.resolve(null)}
        events={[
          {
            topic: "workspace.head-changed",
            payload: { commit_sha: "a" },
            n: 1,
          },
          {
            topic: "workspace.head-changed",
            payload: { commit_sha: "b" },
            n: 2,
          },
          {
            topic: "workspace.head-changed",
            payload: { commit_sha: "c" },
            n: 3,
          },
        ]}
      />,
    );

    await waitFor(() => {
      const sent = (
        posted as { kind: string; payload?: { commit_sha?: string } }[]
      ).filter((m) => m.kind === "event");
      expect(sent).toHaveLength(1);
      expect(sent[0].payload?.commit_sha).toBe("c");
    });
  });
});

describe("settings reach the frame", () => {
  it("arrive in `hello`, separately from the launch parameters", async () => {
    // The backend's `set_extension_settings` has always said the
    // values "reach the extension as launch params on its next
    // mount". Nothing passed them, so a setting could be declared,
    // validated, stored, and never seen by the thing it configures.
    const { posted, frame } = mountFrame({
      params: { doc: "from-the-link" },
      settings: { doc: "from-the-admin", retries: 3 },
    });
    frame?.dispatchEvent(new Event("load"));

    await waitFor(() => {
      const hello = (
        posted as {
          kind: string;
          params?: Record<string, unknown>;
          settings?: Record<string, unknown>;
        }[]
      ).find((m) => m.kind === "hello");
      expect(hello).toBeTruthy();
      // Two namespaces, not one. `params` is where you are and
      // `settings` is how you are set up; merged, the key `doc`
      // would be whichever the host happened to write last.
      expect(hello?.params).toEqual({ doc: "from-the-link" });
      expect(hello?.settings).toEqual({ doc: "from-the-admin", retries: 3 });
    });
  });

  it("are an empty object when the extension declares none", async () => {
    const { posted, frame } = mountFrame();
    frame?.dispatchEvent(new Event("load"));
    await waitFor(() => {
      const hello = (posted as { kind: string; settings?: unknown }[]).find(
        (m) => m.kind === "hello",
      );
      expect(hello?.settings).toEqual({});
    });
  });

  it("a greeting without them is still a greeting", () => {
    // Added after the envelope shipped. A guest built against this
    // version may be framed by a host that predates it, and
    // refusing the hello would turn an additive field into a
    // version wall.
    expect(
      isHostMessage({
        igni: BRIDGE_VERSION,
        kind: "hello",
        nonce: "n",
        extensionId: "acme.scanner",
        params: {},
        theme: "dark",
        tokens: {},
      }),
    ).toBe(true);
  });
});
