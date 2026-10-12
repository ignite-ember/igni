// @vitest-environment jsdom
/**
 * Component tests for ``OverflowMenu``.
 *
 * Collapsing four buttons behind one costs discoverability. What it
 * must not also cost is the keyboard, so most of what is asserted
 * here is that the actions are still reachable and still announce
 * what they are.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OverflowMenu, type OverflowAction } from "./OverflowMenu";

// This project does not set ``globals: true``, so testing-library's
// automatic afterEach cleanup is never installed and renders would
// otherwise accumulate across cases in one document.
afterEach(cleanup);

const actions = (over: Partial<OverflowAction>[] = []): OverflowAction[] => {
  const base: OverflowAction[] = [
    { key: "export", label: "Export", onSelect: vi.fn() },
    { key: "share", label: "Share", onSelect: vi.fn() },
    { key: "remove", label: "Remove", danger: true, onSelect: vi.fn() },
  ];
  return base.map((a, i) => ({ ...a, ...(over[i] ?? {}) }));
};

const open = () => fireEvent.click(screen.getByRole("button", { name: /actions/i }));

describe("the trigger", () => {
  it("says what it is to someone who cannot see three dots", () => {
    render(<OverflowMenu actions={actions()} label="Document actions" />);
    const b = screen.getByRole("button", { name: "Document actions" });
    expect(b.getAttribute("aria-haspopup")).toBe("menu");
    expect(b.getAttribute("aria-expanded")).toBe("false");
  });

  it("renders nothing at all when there is nothing to offer", () => {
    // A dots button that opens an empty box is worse than no button.
    const { container } = render(<OverflowMenu actions={[]} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("the menu", () => {
  it("offers every action once opened", () => {
    render(<OverflowMenu actions={actions()} label="Document actions" />);
    open();
    expect(screen.getByRole("menu")).toBeTruthy();
    expect(screen.getAllByRole("menuitem").map((n) => n.textContent)).toEqual([
      "Export",
      "Share",
      "Remove",
    ]);
  });

  it("runs the action and closes", () => {
    const a = actions();
    render(<OverflowMenu actions={a} label="Document actions" />);
    open();
    fireEvent.click(screen.getByRole("menuitem", { name: "Share" }));

    expect(a[1].onSelect).toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("keeps the destructive action apart from the rest", () => {
    // One slip of the wrist below "Share" should not be "Remove".
    render(<OverflowMenu actions={actions()} label="Document actions" />);
    open();
    expect(screen.getByRole("separator")).toBeTruthy();
  });

  it("will not run a disabled action", () => {
    const a = actions([{ disabled: true }]);
    render(<OverflowMenu actions={a} label="Document actions" />);
    open();
    fireEvent.click(screen.getByRole("menuitem", { name: "Export" }));
    expect(a[0].onSelect).not.toHaveBeenCalled();
  });

  it("says which state a toggle is in rather than only colouring it", () => {
    const a = actions([{}, { active: true, label: "Done comparing" }]);
    render(<OverflowMenu actions={a} label="Document actions" />);
    open();
    expect(
      screen.getByRole("menuitem", { name: "Done comparing" }).getAttribute("aria-current"),
    ).toBe("true");
  });
});

describe("the keyboard", () => {
  it("puts focus on the first action when it opens", () => {
    render(<OverflowMenu actions={actions()} label="Document actions" />);
    open();
    expect(document.activeElement?.textContent).toBe("Export");
  });

  it("skips a disabled action when arrowing", () => {
    const a = actions([{ disabled: true }]);
    render(<OverflowMenu actions={a} label="Document actions" />);
    open();
    expect(document.activeElement?.textContent).toBe("Share");

    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(document.activeElement?.textContent).toBe("Remove");
  });

  it("wraps around the ends", () => {
    // Nothing scrolls in a menu this short, so an arrow that stops
    // dead gives no sign of why it stopped.
    render(<OverflowMenu actions={actions()} label="Document actions" />);
    open();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowUp" });
    expect(document.activeElement?.textContent).toBe("Remove");
  });

  it("closes on Escape and hands focus back to the trigger", () => {
    render(<OverflowMenu actions={actions()} label="Document actions" />);
    open();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });

    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Document actions" }),
    );
  });

  it("keeps Escape from reaching the page underneath", () => {
    // ``PageShell`` listens on the capture phase and navigates back.
    // Closing the menu and leaving the page on one keypress is one
    // thing too many.
    const outer = vi.fn();
    render(<OverflowMenu actions={actions()} label="Document actions" />);
    open();

    document.addEventListener("keydown", outer);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    document.removeEventListener("keydown", outer);

    expect(outer).not.toHaveBeenCalled();
  });
});

describe("submenus", () => {
  const withExport = (): OverflowAction[] => [
    {
      key: "export",
      label: "Export",
      items: [
        { key: "html", label: "HTML", onSelect: vi.fn() },
        { key: "md", label: "Markdown", onSelect: vi.fn() },
      ],
    },
    { key: "share", label: "Share", onSelect: vi.fn() },
  ];

  const openSub = () =>
    fireEvent.click(screen.getByRole("menuitem", { name: "Export" }));

  it("shows the parent, not its contents, until it is opened", () => {
    // The whole point: one Export item instead of three export items.
    render(<OverflowMenu actions={withExport()} label="Document actions" />);
    open();
    expect(screen.getAllByRole("menuitem").map((n) => n.textContent)).toEqual([
      "Export",
      "Share",
    ]);
  });

  it("says it opens another menu", () => {
    render(<OverflowMenu actions={withExport()} label="Document actions" />);
    open();
    const parent = screen.getByRole("menuitem", { name: "Export" });
    expect(parent.getAttribute("aria-haspopup")).toBe("menu");
    expect(parent.getAttribute("aria-expanded")).toBe("false");
  });

  it("reveals the formats once opened", () => {
    render(<OverflowMenu actions={withExport()} label="Document actions" />);
    open();
    openSub();
    expect(screen.getByRole("menuitem", { name: "Export HTML" })).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: "Export Markdown" })).toBeTruthy();
  });

  it("runs the chosen format and closes the whole menu", () => {
    const a = withExport();
    render(<OverflowMenu actions={a} label="Document actions" />);
    open();
    openSub();
    fireEvent.click(screen.getByRole("menuitem", { name: "Export Markdown" }));

    expect(a[0].items?.[1].onSelect).toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("carries the parent name into each item's announced label", () => {
    // "PDF" alone says nothing once the submenu that gave it meaning
    // has been navigated past.
    render(<OverflowMenu actions={withExport()} label="Document actions" />);
    open();
    openSub();
    expect(
      screen.getByRole("menuitem", { name: "Export HTML" }).textContent,
    ).toBe("HTML");
  });
});

describe("the submenu keyboard", () => {
  const actionsWithSub = (): OverflowAction[] => [
    {
      key: "export",
      label: "Export",
      items: [
        { key: "html", label: "HTML", onSelect: vi.fn() },
        { key: "md", label: "Markdown", onSelect: vi.fn() },
      ],
    },
    { key: "share", label: "Share", onSelect: vi.fn() },
  ];

  it("steps in on the right arrow and lands on the first format", () => {
    render(<OverflowMenu actions={actionsWithSub()} label="Document actions" />);
    open();
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Export" }), {
      key: "ArrowRight",
    });
    expect(document.activeElement?.textContent).toBe("HTML");
  });

  it("steps back out on the left arrow, to the item that opened it", () => {
    render(<OverflowMenu actions={actionsWithSub()} label="Document actions" />);
    open();
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Export" }), {
      key: "ArrowRight",
    });
    fireEvent.keyDown(screen.getByRole("menu", { name: "Export" }), {
      key: "ArrowLeft",
    });
    expect(document.activeElement?.textContent).toContain("Export");
  });

  it("closes one level on Escape, not the whole menu", () => {
    // Escape inside a submenu that dropped the entire menu would make
    // backing out of a mis-click cost two reopens.
    render(<OverflowMenu actions={actionsWithSub()} label="Document actions" />);
    open();
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Export" }), {
      key: "ArrowRight",
    });
    fireEvent.keyDown(screen.getByRole("menu", { name: "Export" }), {
      key: "Escape",
    });

    expect(screen.queryByRole("menu", { name: "Export" })).toBeNull();
    expect(screen.getByRole("menu", { name: "Document actions" })).toBeTruthy();
  });

  it("arrows within the submenu, not the parent list", () => {
    render(<OverflowMenu actions={actionsWithSub()} label="Document actions" />);
    open();
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Export" }), {
      key: "ArrowRight",
    });
    fireEvent.keyDown(screen.getByRole("menu", { name: "Export" }), {
      key: "ArrowDown",
    });
    expect(document.activeElement?.textContent).toBe("Markdown");
  });
});
