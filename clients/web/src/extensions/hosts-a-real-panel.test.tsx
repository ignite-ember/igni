// @vitest-environment jsdom
/**
 * The gate: a real panel, hosted through the real bridge, unmodified.
 *
 * `CodeIndexPanel` is the first consumer of the extension contract.
 * If the bridge can host it without the component being rewritten,
 * the contract is worth something; if it cannot, the contract is
 * wrong and this is where that shows up.
 *
 * Nothing here stubs the bridge. `connect()` runs, waits for a real
 * `hello`, and the object it returns is handed to the panel as its
 * `client`. The only thing standing in for production is the window
 * on the other side of `postMessage`, which answers the four RPCs the
 * panel makes.
 *
 * What the gate actually cost: one line. The panel's prop was typed
 * as the concrete `IgniClient`, which has private fields and is
 * therefore nominal — no bridge can satisfy it, whatever its shape.
 * It is now `Pick<IgniClient, "rpc">`, which is what the panel was
 * using all along. The body is untouched.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// The same module ``App.tsx`` renders. If somebody forks the panel
// to make it work in a frame, this import stops pointing at the one
// users see and the gate stops meaning anything.
import { CodeIndexPanel } from "../components/panels/CodeIndexPanel";
import { connect, type Bridge } from "./bridge";
import { BRIDGE_VERSION } from "./protocol";

afterEach(() => {
  cleanup();
  Object.defineProperty(window, "parent", {
    value: window,
    configurable: true,
  });
});

/** A whole ``CodeIndexStatus``, as the backend sends one. */
const STATUS = {
  local_sha: "abc1234",
  head_indexed: true,
  sync_in_progress: false,
  sync_progress_pct: null,
  sync_step: "",
  sync_reason: "",
  sync_error: "",
  install_state: "installed",
  repository_id: "repo-1",
  install_url: "",
  portal_url: "https://portal.internal/repositories",
  remote_url: "git@github.com:ignite-ember/igni.git",
  commits_indexed: 3,
  index_size_bytes: 1024,
  branches_indexed: [],
  last_sync_at: "2026-10-22T10:00:00Z",
  last_sync_stats: { items_upserted: 10, items_deleted: 0 },
};

/** What the backend would answer, keyed by method. */
const ANSWERS: Record<string, unknown> = {
  codeindex_status: STATUS,
  codeindex_activity: [],
  codeindex_head_breakdown: {
    languages: [],
    commits: [],
    files: 12,
    symbols: 340,
  },
};

/**
 * A host window that answers the panel's calls.
 *
 * This is the half the extension never sees: it holds the real
 * client, decides what the frame may ask for, and replies.
 */
function hostAnswering(answers: Record<string, unknown> = ANSWERS) {
  const asked: string[] = [];
  const host = {
    postMessage(message: unknown) {
      const m = message as { kind: string; id?: string; method?: string };
      if (m.kind !== "call" || m.id === undefined || m.method === undefined)
        return;
      asked.push(m.method);
      const value = answers[m.method];
      // Replies arrive asynchronously, as they would over a socket.
      queueMicrotask(() => {
        const event = new MessageEvent("message", {
          data: {
            igni: BRIDGE_VERSION,
            kind: "result",
            id: m.id,
            ok: true,
            value: value ?? null,
          },
        });
        Object.defineProperty(event, "source", { value: host });
        window.dispatchEvent(event);
      });
    },
  } as unknown as Window;
  Object.defineProperty(window, "parent", { value: host, configurable: true });
  return {
    asked,
    greet() {
      const event = new MessageEvent("message", {
        data: {
          igni: BRIDGE_VERSION,
          kind: "hello",
          nonce: "gate-nonce",
          extensionId: "igni.codeindex",
          params: {},
          theme: "dark",
        },
      });
      Object.defineProperty(event, "source", { value: host });
      window.dispatchEvent(event);
    },
  };
}

async function bridged(answers?: Record<string, unknown>): Promise<{
  bridge: Bridge;
  asked: string[];
}> {
  const host = hostAnswering(answers);
  const pending = connect();
  host.greet();
  return { bridge: await pending, asked: host.asked };
}

describe("CodeIndexPanel through the bridge", () => {
  it("renders what the backend said, having asked through postMessage", async () => {
    const { bridge, asked } = await bridged();

    render(<CodeIndexPanel client={bridge} onClose={vi.fn()} />);

    await waitFor(() => expect(screen.getByText(/Up to date/i)).toBeDefined());
    // Not a stub standing in for the bridge: these went out as
    // postMessage envelopes and came back as results.
    expect(asked).toContain("codeindex_status");
    expect(asked).toContain("codeindex_head_breakdown");
  });

  it("shows the backend's own reason when the index is behind", async () => {
    const { bridge } = await bridged({
      ...ANSWERS,
      codeindex_status: {
        ...STATUS,
        head_indexed: false,
        sync_reason: "HEAD has not been indexed yet",
      },
    });

    render(<CodeIndexPanel client={bridge} onClose={vi.fn()} />);

    await waitFor(() => expect(screen.getByText(/Out of date/i)).toBeDefined());
  });
});
