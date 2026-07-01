// @vitest-environment jsdom
/**
 * Component tests for ``FileRefPicker`` — the project-file picker
 * that resolves ``@<path>`` attachments without uploading content.
 *
 * The trickiest contract is the **stale-result race guard**:
 * a user typing fast can fire multiple ``complete_files`` RPCs
 * in quick succession. Whichever resolves last must win
 * regardless of which RPC was last fired — the source uses a
 * ``seq`` counter to drop stale responses. Pin it.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FileRefPicker } from "./FileRefPicker";

interface CompleteFilesResp {
  matches: string[];
  total: number;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function makeClient(
  completeFiles: (q: string) => Promise<CompleteFilesResp>,
) {
  return { completeFiles } as unknown as import("../protocol/client").EmberClient;
}

describe("FileRefPicker — initial render", () => {
  it("focuses the search input on mount", async () => {
    // The picker is a keyboard-first affordance — without
    // autofocus, the user has to click before they can type.
    const client = makeClient(() => Promise.resolve({ matches: [], total: 0 }));
    render(
      <FileRefPicker
        client={client}
        onPick={() => undefined}
        onCancel={() => undefined}
      />,
    );
    await waitFor(() => {
      expect(document.activeElement?.tagName).toBe("INPUT");
    });
  });

  it("fires an initial completeFiles call with empty query", () => {
    // The empty-query call seeds the result list with recent /
    // top-level files so the picker isn't empty before the
    // user types.
    const completeFiles = vi
      .fn<(q: string) => Promise<CompleteFilesResp>>()
      .mockResolvedValue({ matches: [], total: 0 });
    const client = makeClient(completeFiles);
    render(<FileRefPicker client={client} onPick={() => undefined} onCancel={() => undefined} />);
    expect(completeFiles).toHaveBeenCalledWith("", 30);
  });

  it("shows 'No matches.' when the result set is empty", async () => {
    const client = makeClient(() => Promise.resolve({ matches: [], total: 0 }));
    render(
      <FileRefPicker
        client={client}
        onPick={() => undefined}
        onCancel={() => undefined}
      />,
    );
    await waitFor(() => {
      expect(screen.getByText("No matches.")).toBeTruthy();
    });
  });
});

describe("FileRefPicker — query → results", () => {
  it("typing fires completeFiles with the new query", async () => {
    const completeFiles = vi
      .fn<(q: string) => Promise<CompleteFilesResp>>()
      .mockResolvedValue({ matches: ["src/foo.py"], total: 1 });
    const client = makeClient(completeFiles);
    render(<FileRefPicker client={client} onPick={() => undefined} onCancel={() => undefined} />);
    fireEvent.change(screen.getByPlaceholderText(/Search files/), {
      target: { value: "foo" },
    });
    await waitFor(() => {
      expect(completeFiles).toHaveBeenCalledWith("foo", 30);
    });
  });

  it("renders the result rows from the RPC response", async () => {
    const client = makeClient(() =>
      Promise.resolve({ matches: ["src/a.py", "src/b.py"], total: 2 }),
    );
    render(
      <FileRefPicker
        client={client}
        onPick={() => undefined}
        onCancel={() => undefined}
      />,
    );
    await waitFor(() => {
      expect(screen.getByText("src/a.py")).toBeTruthy();
      expect(screen.getByText("src/b.py")).toBeTruthy();
    });
  });

  it("stale RPC responses are discarded (race guard)", async () => {
    // The picker fires one RPC per keystroke. If the user types
    // fast, the OLDER RPC may resolve AFTER the newer one. The
    // ``seq`` counter in the source drops stale responses; pin
    // it because the bug would only show on slow networks.
    let resolveFirst: (v: CompleteFilesResp) => void = () => undefined;
    let resolveSecond: (v: CompleteFilesResp) => void = () => undefined;
    const completeFiles = vi
      .fn<(q: string) => Promise<CompleteFilesResp>>()
      .mockImplementationOnce(
        () => new Promise<CompleteFilesResp>((r) => (resolveFirst = r)),
      )
      .mockImplementationOnce(
        () => new Promise<CompleteFilesResp>((r) => (resolveSecond = r)),
      );
    const client = makeClient(completeFiles);
    render(<FileRefPicker client={client} onPick={() => undefined} onCancel={() => undefined} />);
    // First RPC (empty query) is in flight. Type "f" — that
    // fires the second RPC.
    fireEvent.change(screen.getByPlaceholderText(/Search files/), {
      target: { value: "f" },
    });
    await waitFor(() => {
      expect(completeFiles).toHaveBeenCalledTimes(2);
    });
    // Resolve them OUT OF ORDER — second (current) first,
    // then first (stale). The stale one must be ignored.
    resolveSecond({ matches: ["FRESH_RESULT"], total: 1 });
    await waitFor(() => {
      expect(screen.getByText("FRESH_RESULT")).toBeTruthy();
    });
    resolveFirst({ matches: ["STALE_RESULT"], total: 1 });
    // Wait a beat for any erroneous re-render to land.
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText("STALE_RESULT")).toBeNull();
    expect(screen.getByText("FRESH_RESULT")).toBeTruthy();
  });

  it("RPC failure clears the results (rather than showing stale ones)", async () => {
    // Catch-with-empty-array on failure — better to show "No
    // matches" than to leave the prior results stuck up there
    // pretending to still be relevant.
    const completeFiles = vi
      .fn<(q: string) => Promise<CompleteFilesResp>>()
      .mockResolvedValueOnce({ matches: ["old"], total: 1 })
      .mockRejectedValueOnce(new Error("transport down"));
    const client = makeClient(completeFiles);
    render(<FileRefPicker client={client} onPick={() => undefined} onCancel={() => undefined} />);
    await waitFor(() => expect(screen.getByText("old")).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText(/Search files/), {
      target: { value: "x" },
    });
    await waitFor(() => {
      expect(screen.queryByText("old")).toBeNull();
      expect(screen.getByText("No matches.")).toBeTruthy();
    });
  });
});

describe("FileRefPicker — keyboard navigation", () => {
  async function setup(matches: string[]) {
    const onPick = vi.fn();
    const onCancel = vi.fn();
    const client = makeClient(() => Promise.resolve({ matches, total: matches.length }));
    render(<FileRefPicker client={client} onPick={onPick} onCancel={onCancel} />);
    if (matches.length > 0) await screen.findByText(matches[0]);
    return {
      input: screen.getByPlaceholderText(/Search files/),
      onPick,
      onCancel,
    };
  }

  it("Arrow Down advances the active index, capped at the last row", async () => {
    const { input } = await setup(["a", "b", "c"]);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    // 3rd row is now active.
    expect(
      Array.from(document.querySelectorAll(".file-ref-item")).map((el) =>
        el.className.includes("active") ? "*" : "-",
      ),
    ).toEqual(["-", "-", "*"]);
    // Cap at last — extra Down doesn't wrap.
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(
      Array.from(document.querySelectorAll(".file-ref-item")).map((el) =>
        el.className.includes("active") ? "*" : "-",
      ),
    ).toEqual(["-", "-", "*"]);
  });

  it("Arrow Up reverses, floored at 0", async () => {
    const { input } = await setup(["a", "b"]);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "ArrowUp" }); // already at 0
    expect(
      Array.from(document.querySelectorAll(".file-ref-item")).map((el) =>
        el.className.includes("active") ? "*" : "-",
      ),
    ).toEqual(["*", "-"]);
  });

  it("Enter picks the active result via onPick", async () => {
    const { input, onPick } = await setup(["src/a.py", "src/b.py"]);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPick).toHaveBeenCalledWith("src/b.py");
  });

  it("Esc cancels", async () => {
    const { input, onCancel } = await setup(["a"]);
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("Enter with no results is a no-op (no onPick)", async () => {
    const { input, onPick } = await setup([]);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPick).not.toHaveBeenCalled();
  });
});

describe("FileRefPicker — mouse interactions", () => {
  async function setup(matches: string[]) {
    const onPick = vi.fn();
    const client = makeClient(() => Promise.resolve({ matches, total: matches.length }));
    render(<FileRefPicker client={client} onPick={onPick} onCancel={() => undefined} />);
    await screen.findByText(matches[0]);
    return { onPick };
  }

  it("mouseEnter on a row sets it active", async () => {
    await setup(["a", "b", "c"]);
    fireEvent.mouseEnter(screen.getByText("c"));
    expect(screen.getByText("c").className).toContain("active");
  });

  it("mouseDown (not click) picks — preserves composer focus", async () => {
    // The picker is shown above the composer; if pick fires on
    // ``click`` it loses to the input's blur event and the
    // composer's @-mention popup state never updates. Using
    // ``mousedown`` (which fires before blur) is the
    // load-bearing pattern.
    const { onPick } = await setup(["src/foo.py"]);
    fireEvent.mouseDown(screen.getByText("src/foo.py"));
    expect(onPick).toHaveBeenCalledWith("src/foo.py");
  });

  it("backdrop click cancels", async () => {
    const onCancel = vi.fn();
    const client = makeClient(() => Promise.resolve({ matches: [], total: 0 }));
    const { container } = render(
      <FileRefPicker
        client={client}
        onPick={() => undefined}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(container.querySelector(".drawer-backdrop") as HTMLElement);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
