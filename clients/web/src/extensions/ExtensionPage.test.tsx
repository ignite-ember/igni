// @vitest-environment jsdom
/**
 * Mounting an extension, and what is shown when it cannot be.
 *
 * `ExtensionFrame` had been built and tested for months against a
 * stub window and was never mounted, because mounting it needs app
 * knowledge the frame deliberately does not have: which extension a
 * page belongs to, what state it is in, and where its UI is served.
 * This is that layer, so most of what is tested here is the states
 * where there is no frame to draw.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ExtensionPage, type ExtensionSummary } from "./ExtensionPage";
import { readDocumentTheme } from "./useDocumentTheme";

afterEach(cleanup);

const READY: ExtensionSummary = {
  id: "acme.scanner",
  name: "Compliance Scanner",
  version: "1.0.0",
  state: "READY",
  reason: "",
  origin: "http://127.0.0.1:41234",
  allow: ["ext.acme.scanner.*"],
  pages: [
    { kind: "acme.scan", label: "Compliance", sidebar: true, entry: "index.html" },
  ],
  menu: [],
  indicators: [],
  settings: [],
  values: {},
};

function draw(extension: Partial<ExtensionSummary> = {}, kind = "acme.scan") {
  return render(
    <ExtensionPage
      extension={{ ...READY, ...extension }}
      kind={kind}
      onCall={() => Promise.resolve(null)}
    />,
  );
}

describe("a READY extension", () => {
  it("is framed at its own origin and entry", () => {
    const { container } = draw();
    const frame = container.querySelector("iframe");
    expect(frame?.getAttribute("src")).toBe("http://127.0.0.1:41234/index.html");
  });

  it("uses the page's entry, not always index.html", () => {
    const { container } = draw({
      pages: [
        { kind: "acme.scan", label: "C", sidebar: true, entry: "scan.html" },
      ],
    });
    expect(container.querySelector("iframe")?.getAttribute("src")).toBe(
      "http://127.0.0.1:41234/scan.html",
    );
  });

  it("names the frame after the page, for the accessibility tree", () => {
    const { container } = draw();
    expect(container.querySelector("iframe")?.getAttribute("title")).toBe(
      "Compliance",
    );
  });
});

describe("an extension that cannot be drawn", () => {
  it("says why it is FAILED, with the reason the backend gave", () => {
    // "FAILED" alone sends an administrator to the logs. The health
    // contract requires a reason that names a fix, and this is where
    // it is read.
    const { container } = draw({
      state: "FAILED",
      reason: "Licence expired; install the renewed key.",
    });
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByText(/Licence expired/)).not.toBeNull();
  });

  it("shows FAILED even with no reason, rather than nothing", () => {
    draw({ state: "FAILED", reason: "" });
    expect(screen.getByText(/is FAILED/)).not.toBeNull();
  });

  it("says it is starting while PREPARING", () => {
    const { container } = draw({ state: "PREPARING" });
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("starting");
  });

  it("does not frame a READY extension whose UI is not served", () => {
    // The installer marks that FAILED, so this is a guard against a
    // backend disagreeing with itself rather than an expected state.
    const { container } = draw({ origin: "" });
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByText(/not being served/)).not.toBeNull();
  });

  it("says so when the kind is not one of its pages", () => {
    const { container } = draw({}, "acme.nope");
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByText(/does not contribute a page/)).not.toBeNull();
  });

  it("offers a way back out of a dead end", () => {
    const onClose = vi.fn();
    render(
      <ExtensionPage
        extension={{ ...READY, state: "FAILED", reason: "broken" }}
        kind="acme.scan"
        onCall={() => Promise.resolve(null)}
        onClose={onClose}
      />,
    );
    screen.getByRole("button", { name: "Back" }).click();
    expect(onClose).toHaveBeenCalledOnce();
  });
});

describe("readDocumentTheme", () => {
  afterEach(() => {
    delete document.documentElement.dataset.theme;
  });

  it("prefers an explicit attribute", () => {
    document.documentElement.dataset.theme = "light";
    expect(readDocumentTheme()).toBe("light");
  });

  it("falls back to the OS preference", () => {
    // jsdom's matchMedia is absent by default, which is itself the
    // case worth covering: it must not throw.
    delete document.documentElement.dataset.theme;
    expect(["dark", "light"]).toContain(readDocumentTheme());
  });

  it("ignores an attribute that is not a theme", () => {
    document.documentElement.dataset.theme = "neon";
    expect(["dark", "light"]).toContain(readDocumentTheme());
  });
});
