// @vitest-environment jsdom
/**
 * Component tests for ``FilePreview`` — plain-browser fallback for
 * ``host.openFile()`` when no native IDE bridge is available.
 *
 * The component owns an RPC round-trip (``read_file``) and three
 * visible states (loading / success / error). Pin each state +
 * the dismissal paths (Esc, backdrop, close button).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FilePreview } from "./FilePreview";

interface ReadFileResp {
  path: string;
  contents: string;
  size: number;
  language?: string;
  error?: string;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/** Minimal stub of the EmberClient surface that FilePreview uses.
 *  Only ``rpc`` is exercised. */
function makeClient(
  rpc: (method: string, args: { path: string }) => Promise<ReadFileResp>,
) {
  return { rpc } as unknown as import("../protocol/client").EmberClient;
}

describe("FilePreview — loading / success / error", () => {
  it("shows the loading state until the RPC resolves", async () => {
    let resolveRpc: (v: ReadFileResp) => void = () => undefined;
    const client = makeClient(() => new Promise<ReadFileResp>((r) => (resolveRpc = r)));
    render(<FilePreview client={client} path="src/x.py" onClose={() => undefined} />);
    // ``Loading…`` is the placeholder copy while the RPC is in
    // flight — important for a slow disk / large file.
    expect(screen.getByText("Loading…")).toBeTruthy();
    // Release the RPC so the cleanup doesn't leak.
    resolveRpc({ path: "src/x.py", contents: "def x(): pass", size: 13 });
    await waitFor(() => expect(screen.queryByText("Loading…")).toBeNull());
  });

  it("renders the file contents in a pre+code block on success", async () => {
    const client = makeClient(() =>
      Promise.resolve({ path: "src/x.py", contents: "print('hi')", size: 11 }),
    );
    const { container } = render(
      <FilePreview client={client} path="src/x.py" onClose={() => undefined} />,
    );
    await waitFor(() => {
      expect(container.querySelector("pre code")?.textContent).toBe(
        "print('hi')",
      );
    });
  });

  it("applies the language class from the RPC response, else falls back to extension", async () => {
    // The ``language`` field wins when set (richer than ext —
    // BE knows e.g. that .gitignore is gitignore syntax). When
    // absent, we fall back to the extension.
    const client = makeClient(() =>
      Promise.resolve({
        path: "src/x.py",
        contents: "print(1)",
        size: 8,
        language: "python",
      }),
    );
    const { container } = render(
      <FilePreview client={client} path="src/x.py" onClose={() => undefined} />,
    );
    await waitFor(() => {
      const pre = container.querySelector("pre");
      expect(pre?.className).toContain("lang-python");
    });
  });

  it("renders the error message when read_file rejects", async () => {
    // RPC failure must surface inline (not just empty pre). The
    // user is trying to look at a file; "permission denied" or
    // "file too large" is the signal they need.
    const client = makeClient(() => Promise.reject(new Error("permission denied")));
    render(<FilePreview client={client} path="src/secret.env" onClose={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByText("permission denied")).toBeTruthy();
    });
  });

  it("renders the BE-returned error field when the response carries one", async () => {
    // The BE sometimes returns success-with-error (e.g. binary
    // file refusal) instead of throwing. Same UX — surface it.
    const client = makeClient(() =>
      Promise.resolve({
        path: "img.png",
        contents: "",
        size: 0,
        error: "binary file",
      }),
    );
    render(<FilePreview client={client} path="img.png" onClose={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByText("binary file")).toBeTruthy();
    });
  });
});

describe("FilePreview — dismissal", () => {
  it("Esc closes the preview", async () => {
    const onClose = vi.fn();
    const client = makeClient(() =>
      Promise.resolve({ path: "x", contents: "y", size: 1 }),
    );
    render(<FilePreview client={client} path="x" onClose={onClose} />);
    // Esc listener is on ``document``, not the preview element.
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("backdrop click closes the preview", () => {
    const onClose = vi.fn();
    const client = makeClient(() =>
      Promise.resolve({ path: "x", contents: "y", size: 1 }),
    );
    const { container } = render(
      <FilePreview client={client} path="x" onClose={onClose} />,
    );
    fireEvent.click(container.querySelector(".drawer-backdrop") as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("close button closes the preview", () => {
    const onClose = vi.fn();
    const client = makeClient(() =>
      Promise.resolve({ path: "x", contents: "y", size: 1 }),
    );
    render(<FilePreview client={client} path="x" onClose={onClose} />);
    fireEvent.click(screen.getByTitle("Close (Esc)"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("FilePreview — Copy path", () => {
  it("Copy button writes the resolved path to the clipboard", async () => {
    // After the RPC lands, the displayed path uses the
    // BE-returned canonical form (e.g. absolute-resolved
    // symlinks). The copy uses that, not the original prop.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const client = makeClient(() =>
      Promise.resolve({
        path: "/abs/canonical/x.py",
        contents: "",
        size: 0,
      }),
    );
    render(<FilePreview client={client} path="x.py" onClose={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByTitle("Copy path")).toBeTruthy();
    });
    fireEvent.click(screen.getByTitle("Copy path"));
    expect(writeText).toHaveBeenCalledWith("/abs/canonical/x.py");
  });
});
