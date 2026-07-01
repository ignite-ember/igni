// @vitest-environment jsdom
/**
 * Component tests for ``Sidebar`` — the session-list drawer.
 *
 * The biggest contract worth pinning: the mobile-only backdrop
 * dismissal. We render the dark overlay ONLY when
 * ``window.innerWidth <= 700``; on desktop the sidebar is part
 * of the layout and a backdrop click should NOT collapse it.
 * Drifting that threshold or dropping the guard makes desktop
 * users lose their sidebar every time they click in the chat.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

// ``vi.hoisted`` runs BEFORE the static imports below. The
// embedded ThemeToggle reads localStorage at module load
// (Node 25's native stub has no methods); install a working
// shim now so the import doesn't crash.
vi.hoisted(() => {
  const data = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return data.size;
    },
    clear() {
      data.clear();
    },
    getItem(key: string) {
      return data.has(key) ? (data.get(key) as string) : null;
    },
    key(i: number) {
      return Array.from(data.keys())[i] ?? null;
    },
    removeItem(key: string) {
      data.delete(key);
    },
    setItem(key: string, value: string) {
      data.set(key, String(value));
    },
  };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    writable: true,
    value: shim,
  });
  // jsdom doesn't ship ResizeObserver — the embedded
  // ScrollIndicator wires one. No-op stub is enough for our
  // tests (we don't depend on observed-size callbacks).
  class FakeResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  Object.defineProperty(window, "ResizeObserver", {
    configurable: true,
    writable: true,
    value: FakeResizeObserver,
  });
  (globalThis as unknown as { ResizeObserver: typeof FakeResizeObserver }).ResizeObserver =
    FakeResizeObserver;
});

import { Sidebar, type SessionEntry } from "./Sidebar";

afterEach(() => {
  cleanup();
});

function setInnerWidth(px: number) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: px,
  });
}

// jsdom doesn't ship matchMedia (ThemeToggle uses it); install
// a permissive stub so the embedded ThemeToggle doesn't crash.
beforeEach(() => {
  setInnerWidth(1200); // desktop default
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: () =>
      ({
        matches: false,
        media: "",
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
        onchange: null,
      }) as unknown as MediaQueryList,
  });
});

const sessions: SessionEntry[] = [
  { session_id: "abcd1234ef", name: "Alpha", detail: "Detailed alpha" },
  { session_id: "wxyz5678", name: "Beta" },
];

function defaultProps(overrides: Partial<React.ComponentProps<typeof Sidebar>> = {}) {
  return {
    open: true,
    sessions,
    currentId: "abcd1234ef",
    onNewChat: vi.fn(),
    onPick: vi.fn(),
    onClose: vi.fn(),
    ...overrides,
  };
}

describe("Sidebar — open/closed state", () => {
  it("applies the closed class when open=false", () => {
    // CSS uses ``.sidebar.closed`` to slide the drawer off-screen.
    // No JS-level visibility — the class IS the toggle.
    const { container } = render(<Sidebar {...defaultProps({ open: false })} />);
    expect(container.querySelector(".sidebar")?.className).toContain("closed");
  });

  it("does not apply closed class when open", () => {
    const { container } = render(<Sidebar {...defaultProps({ open: true })} />);
    expect(container.querySelector(".sidebar")?.className).not.toContain(
      "closed",
    );
  });
});

describe("Sidebar — session list", () => {
  it("shows the empty placeholder when sessions list is empty", () => {
    // ``No past sessions`` is the only row when nothing has
    // been persisted. Pin the copy so a rename doesn't
    // confuse first-launch users.
    render(<Sidebar {...defaultProps({ sessions: [] })} />);
    expect(screen.getByText("No past sessions")).toBeTruthy();
  });

  it("renders one row per session with name + short id", () => {
    render(<Sidebar {...defaultProps()} />);
    // Name (or session_id as fallback if name is empty).
    expect(screen.getByText("Alpha")).toBeTruthy();
    expect(screen.getByText("Beta")).toBeTruthy();
    // Short 8-char id appears alongside name — same prefix
    // convention as SessionChip uses.
    expect(screen.getByText("abcd1234")).toBeTruthy();
    expect(screen.getByText("wxyz5678")).toBeTruthy();
  });

  it("uses session_id as the display name when name is blank", () => {
    // Recently forked sessions may not have a name yet —
    // showing an empty row is unusable, so fall back to the
    // full id (the prefix shown to the right is the same
    // string, but the row body needs SOMETHING).
    render(
      <Sidebar
        {...defaultProps({
          sessions: [{ session_id: "no-name-id", name: "" }],
        })}
      />,
    );
    expect(screen.getAllByText(/no-name-id/).length).toBeGreaterThan(0);
  });

  it("flags the current session with the .current class", () => {
    // The active conversation needs a visual anchor in the
    // list. Pin the class because the styling is the only
    // affordance.
    const { container } = render(
      <Sidebar {...defaultProps({ currentId: "wxyz5678" })} />,
    );
    const items = container.querySelectorAll(".session-item");
    const currents = Array.from(items).filter((el) =>
      el.className.includes("current"),
    );
    expect(currents).toHaveLength(1);
    expect(currents[0].textContent).toContain("Beta");
  });

  it("uses detail as the hover title, falling back to name", () => {
    // ``detail`` is the longer one-line preview the BE
    // generates from the first user message. When absent, the
    // session name is the next-best tooltip.
    const { container } = render(<Sidebar {...defaultProps()} />);
    const items = container.querySelectorAll(".session-item");
    expect(items[0].getAttribute("title")).toBe("Detailed alpha"); // detail wins
    expect(items[1].getAttribute("title")).toBe("Beta"); // name fallback
  });

  it("clicking a session calls onPick with its id", () => {
    const onPick = vi.fn();
    render(<Sidebar {...defaultProps({ onPick })} />);
    fireEvent.click(screen.getByText("Beta"));
    expect(onPick).toHaveBeenCalledWith("wxyz5678");
  });

  it("New chat button calls onNewChat", () => {
    const onNewChat = vi.fn();
    render(<Sidebar {...defaultProps({ onNewChat })} />);
    fireEvent.click(screen.getByRole("button", { name: "+ New chat" }));
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });
});

describe("Sidebar — backdrop / mobile dismissal", () => {
  it("renders the backdrop overlay when open AND innerWidth ≤ 700", () => {
    // On mobile, the sidebar overlays the chat — a backdrop
    // is the standard affordance to close it. On desktop the
    // sidebar sits side-by-side and a backdrop would block
    // the chat unexpectedly.
    setInnerWidth(600);
    const { container } = render(<Sidebar {...defaultProps()} />);
    // The backdrop is an inline-styled div before the <nav>.
    const allDivs = container.querySelectorAll("div");
    const backdrop = Array.from(allDivs).find(
      (d) => (d as HTMLElement).style.position === "fixed",
    );
    expect(backdrop).toBeTruthy();
  });

  it("does NOT render the backdrop on desktop (> 700px) even when open", () => {
    setInnerWidth(1200);
    const { container } = render(<Sidebar {...defaultProps()} />);
    const allDivs = container.querySelectorAll("div");
    const backdrop = Array.from(allDivs).find(
      (d) => (d as HTMLElement).style.position === "fixed",
    );
    expect(backdrop).toBeFalsy();
  });

  it("clicking the mobile backdrop calls onClose", () => {
    setInnerWidth(600);
    const onClose = vi.fn();
    const { container } = render(<Sidebar {...defaultProps({ onClose })} />);
    const allDivs = container.querySelectorAll("div");
    const backdrop = Array.from(allDivs).find(
      (d) => (d as HTMLElement).style.position === "fixed",
    ) as HTMLElement;
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("backdrop is absent when sidebar is closed (no zombie overlay)", () => {
    // Closed sidebar + small screen — must NOT leave the
    // backdrop hanging (it would block the chat with no
    // visible reason).
    setInnerWidth(600);
    const { container } = render(
      <Sidebar {...defaultProps({ open: false })} />,
    );
    const allDivs = container.querySelectorAll("div");
    const backdrop = Array.from(allDivs).find(
      (d) => (d as HTMLElement).style.position === "fixed",
    );
    expect(backdrop).toBeFalsy();
  });
});
