// @vitest-environment jsdom
/**
 * Component tests for ``PageShell`` — the breadcrumb trail.
 *
 * The trail is assembled from two sources that meet only here: the
 * page stack (which destination you are in) and the panel's own depth
 * (where you are inside it). Which one a crumb belongs to decides what
 * clicking it does, and getting that wrong is invisible until you
 * click — the first version of this silently dropped the panel's half
 * of the trail entirely, and only driving the real app showed it.
 *
 * Contracts worth pinning:
 *   • Both halves render, in order, under a Chat root.
 *   • The destination stops being the current crumb once the panel is
 *     deeper than its own root — and clicking it then asks the *panel*
 *     to go back, not the stack.
 *   • Esc pops one crumb, whichever half it came from.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { PageShell } from "./PageShell";
import type { PageRoute } from "../chat/pageStack";

// This project does not set ``globals: true``, so testing-library's
// automatic afterEach cleanup is never installed and renders would
// otherwise accumulate across cases in one document.
afterEach(cleanup);

const trail: PageRoute[] = [{ kind: "plugins", label: "Plugins" }];

function setup(overrides: Partial<Parameters<typeof PageShell>[0]> = {}) {
  const props = {
    trail,
    onCrumb: vi.fn(),
    onBack: vi.fn(),
    onClose: vi.fn(),
    children: <div>body</div>,
    ...overrides,
  };
  render(<PageShell {...props} />);
  return props;
}

const crumbText = () =>
  Array.from(document.querySelectorAll(".page-crumb")).map((e) => e.textContent);

describe("the trail", () => {
  it("is Chat then the destination when the panel is at its root", () => {
    setup();
    expect(crumbText()).toEqual(["Chat", "Plugins"]);
  });

  it("appends the panel's own depth", () => {
    setup({ levels: { labels: ["acme-tools"], onTruncate: vi.fn() } });
    expect(crumbText()).toEqual(["Chat", "Plugins", "acme-tools"]);
  });

  it("titles the page after the deepest crumb", () => {
    setup({ levels: { labels: ["acme-tools"], onTruncate: vi.fn() } });
    expect(document.querySelector(".page-title")?.textContent).toBe("acme-tools");
  });
});

describe("clicking a crumb", () => {
  it("returns to the chat from the root crumb", () => {
    const props = setup();
    fireEvent.click(screen.getByText("Chat"));
    expect(props.onClose).toHaveBeenCalled();
  });

  it("asks the panel to go back when the destination is not the leaf", () => {
    // The destination and the panel's level are different halves of
    // one trail: only the panel can undo its own selection, so this
    // must not go to the stack.
    const onTruncate = vi.fn();
    const props = setup({ levels: { labels: ["acme-tools"], onTruncate } });
    fireEvent.click(screen.getByText("Plugins"));
    expect(onTruncate).toHaveBeenCalledWith(-1);
    expect(props.onCrumb).not.toHaveBeenCalled();
  });

  it("leaves the deepest crumb unclickable", () => {
    setup({ levels: { labels: ["acme-tools"], onTruncate: vi.fn() } });
    // Scoped to the crumb: the leaf's label is also the page title, so
    // a bare text query matches twice.
    const leaf = document.querySelector(".page-crumb.current");
    expect(leaf?.textContent).toBe("acme-tools");
    expect(leaf?.tagName).toBe("SPAN");
    expect(leaf?.getAttribute("aria-current")).toBe("page");
  });
});

describe("Esc", () => {
  it("pops the panel's level before leaving the destination", () => {
    const onTruncate = vi.fn();
    const props = setup({ levels: { labels: ["acme-tools"], onTruncate } });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onTruncate).toHaveBeenCalledWith(-1);
    expect(props.onBack).not.toHaveBeenCalled();
  });

  it("leaves the destination once the panel is at its root", () => {
    const props = setup();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(props.onBack).toHaveBeenCalled();
  });

  it("defers to an overlay that owns Esc", () => {
    // A file preview sits on top and closes first — same rule
    // ``Drawer`` documents.
    const props = setup();
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    document.body.appendChild(overlay);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(props.onBack).not.toHaveBeenCalled();
    overlay.remove();
  });
});
