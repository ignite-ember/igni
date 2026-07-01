// @vitest-environment jsdom
/**
 * Component tests for ``FilePill`` — inline file reference with
 * a click-to-open action menu.
 *
 * Contracts worth pinning:
 *   • Basename derived from the path is the visible label.
 *   • Click toggles the menu (and clicks-outside / Esc close it).
 *   • Copy → ``navigator.clipboard.writeText(path)`` with the
 *     FULL path (not the basename — file paths are only useful
 *     when complete).
 *   • Open → routes through ``host.openFile`` (the host abstraction
 *     handles VSCode / JetBrains / Tauri differently).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FilePill } from "./FilePill";
import { host } from "../lib/host";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("FilePill — surface render", () => {
  it("renders the basename as the visible label", () => {
    // Visible label is small — full path would overflow the
    // bubble. Basename is the affordance; title attr has the
    // full path for precision.
    render(<FilePill path="/Users/dz/proj/src/foo.py" />);
    expect(screen.getByText("foo.py")).toBeTruthy();
  });

  it("title attr carries the full path", () => {
    const { container } = render(<FilePill path="/abs/long/path/to/file.ts" />);
    expect(container.querySelector(".file-pill")?.getAttribute("title")).toBe(
      "/abs/long/path/to/file.ts",
    );
  });

  it("falls back to the whole path when there's no separator", () => {
    // Bare filename or unusual identifier — show what we have
    // rather than emptying out.
    render(<FilePill path="bare-name.md" />);
    expect(screen.getByText("bare-name.md")).toBeTruthy();
  });
});

describe("FilePill — menu open/close", () => {
  it("menu is hidden initially", () => {
    const { container } = render(<FilePill path="x.py" />);
    expect(container.querySelector(".file-pill-menu")).toBeNull();
  });

  it("clicking the pill opens the menu", () => {
    const { container } = render(<FilePill path="x.py" />);
    fireEvent.click(container.querySelector(".file-pill") as HTMLElement);
    expect(container.querySelector(".file-pill-menu")).toBeTruthy();
    // Menu shows the full path + Copy / Open buttons.
    expect(screen.getByText("Copy path")).toBeTruthy();
  });

  it("clicking the pill again toggles the menu closed", () => {
    const { container } = render(<FilePill path="x.py" />);
    const pill = container.querySelector(".file-pill") as HTMLElement;
    fireEvent.click(pill);
    expect(container.querySelector(".file-pill-menu")).toBeTruthy();
    fireEvent.click(pill);
    expect(container.querySelector(".file-pill-menu")).toBeNull();
  });

  it("Escape closes an open menu", () => {
    // Standard keyboard-dismissal contract — without it, a
    // user who opened the menu by accident can't easily close
    // it from the keyboard.
    const { container } = render(<FilePill path="x.py" />);
    fireEvent.click(container.querySelector(".file-pill") as HTMLElement);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(container.querySelector(".file-pill-menu")).toBeNull();
  });

  it("clicking outside the wrap closes the menu", () => {
    // The component listens on ``window.mousedown`` for clicks
    // outside its wrapper. Pin both that the listener exists
    // and that an outside click closes — the inside click
    // (toggle) is tested above.
    const { container } = render(
      <div>
        <FilePill path="x.py" />
        <button data-testid="outside">somewhere else</button>
      </div>,
    );
    fireEvent.click(container.querySelector(".file-pill") as HTMLElement);
    expect(container.querySelector(".file-pill-menu")).toBeTruthy();
    // Dispatch a real mousedown on the outside element — the
    // window listener picks it up.
    fireEvent.mouseDown(screen.getByTestId("outside"));
    expect(container.querySelector(".file-pill-menu")).toBeNull();
  });
});

describe("FilePill — copy path", () => {
  it("Copy path writes the FULL path to the clipboard", async () => {
    // The visible label is the basename, but Copy must yield
    // the absolute path — otherwise paste-into-shell breaks.
    // Same load-bearing distinction as SessionChip's short
    // prefix vs full-id copy.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const { container } = render(
      <FilePill path="/Users/dz/proj/src/foo.py" />,
    );
    fireEvent.click(container.querySelector(".file-pill") as HTMLElement);
    fireEvent.click(screen.getByText("Copy path"));
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith("/Users/dz/proj/src/foo.py");
  });

  it("Copy click closes the menu", () => {
    // After a successful copy, the menu has done its job —
    // close so the user gets feedback that the click landed.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const { container } = render(<FilePill path="x.py" />);
    fireEvent.click(container.querySelector(".file-pill") as HTMLElement);
    fireEvent.click(screen.getByText("Copy path"));
    expect(container.querySelector(".file-pill-menu")).toBeNull();
  });
});

describe("FilePill — Open / Preview routing", () => {
  it("clicking Open / Preview routes through host.openFile with the full path", () => {
    // The button label flips per host (Open on IDEs, Preview
    // on web), but the action is the same — delegate to the
    // host abstraction. Without this routing, IDE users can't
    // click their attachment to land in the editor.
    const openFile = vi.spyOn(host, "openFile").mockResolvedValue(true);
    const { container } = render(<FilePill path="/full/path/x.py" />);
    fireEvent.click(container.querySelector(".file-pill") as HTMLElement);
    // Match by role + flexible name because the label varies.
    const btn = screen.getByRole("button", { name: /Open|Preview/ });
    fireEvent.click(btn);
    expect(openFile).toHaveBeenCalledWith("/full/path/x.py");
  });

  it("clicking Open / Preview closes the menu", () => {
    vi.spyOn(host, "openFile").mockResolvedValue(true);
    const { container } = render(<FilePill path="x.py" />);
    fireEvent.click(container.querySelector(".file-pill") as HTMLElement);
    fireEvent.click(screen.getByRole("button", { name: /Open|Preview/ }));
    expect(container.querySelector(".file-pill-menu")).toBeNull();
  });
});
