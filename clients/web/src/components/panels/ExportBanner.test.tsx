// @vitest-environment jsdom
/**
 * Component tests for ``ExportBanner``.
 *
 * The banner exists because the export result used to be a line of
 * text inside the document body: it pushed the page down by a row and
 * left the path to be copied out by hand. So what is pinned here is
 * that the path is actionable and that a failure reads as one.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ExportBanner, fileNameOf, folderOf } from "./ExportBanner";

afterEach(cleanup);

const ok = { path: "/Users/maya/proj/auth-design-v3.pdf", format: "pdf", error: "" };

describe("splitting the path", () => {
  it("separates the file from the folder it landed in", () => {
    expect(fileNameOf(ok.path)).toBe("auth-design-v3.pdf");
    expect(folderOf(ok.path)).toBe("/Users/maya/proj");
  });

  it("handles a windows path", () => {
    expect(fileNameOf("C:\\Users\\maya\\auth-v3.md")).toBe("auth-v3.md");
    expect(folderOf("C:\\Users\\maya\\auth-v3.md")).toBe("C:\\Users\\maya");
  });

  it("survives a bare filename with no folder at all", () => {
    expect(fileNameOf("auth.md")).toBe("auth.md");
    expect(folderOf("auth.md")).toBe("");
  });
});

describe("a finished export", () => {
  it("names the format and the file", () => {
    render(<ExportBanner outcome={ok} onOpen={() => {}} onDismiss={() => {}} />);
    expect(screen.getByText("Exported as PDF")).toBeTruthy();
    expect(screen.getByText("auth-design-v3.pdf")).toBeTruthy();
  });

  it("offers to open it rather than leaving a path to copy out", () => {
    const onOpen = vi.fn();
    render(<ExportBanner outcome={ok} onOpen={onOpen} onDismiss={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(onOpen).toHaveBeenCalled();
  });

  it("says so while the open is in flight", () => {
    render(<ExportBanner outcome={ok} busy onOpen={() => {}} onDismiss={() => {}} />);
    const button = screen.getByRole("button", { name: "Opening…" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps the full path reachable when the folder is truncated", () => {
    // The folder line is one ellipsised row; the whole path has to
    // stay available or a deep export becomes unfindable.
    const { container } = render(
      <ExportBanner outcome={ok} onOpen={() => {}} onDismiss={() => {}} />,
    );
    expect(container.querySelector("[title='/Users/maya/proj/auth-design-v3.pdf']")).toBeTruthy();
  });
});

describe("a failed export", () => {
  const failed = {
    path: "",
    format: "pdf",
    error: "PDF export needs the pango graphics libraries.",
  };

  it("names the format that failed and why", () => {
    render(<ExportBanner outcome={failed} onOpen={() => {}} onDismiss={() => {}} />);
    expect(screen.getByText("PDF export failed")).toBeTruthy();
    expect(screen.getByText(/pango graphics libraries/)).toBeTruthy();
  });

  it("offers no Open, because there is nothing to open", () => {
    render(<ExportBanner outcome={failed} onOpen={() => {}} onDismiss={() => {}} />);
    expect(screen.queryByRole("button", { name: "Open" })).toBeNull();
  });

  it("is still only a status, not an alert", () => {
    // ``alert`` interrupts a screen reader mid-sentence. A file that
    // did not get written is not that urgent.
    const { container } = render(
      <ExportBanner outcome={failed} onOpen={() => {}} onDismiss={() => {}} />,
    );
    expect(container.querySelector("[role='status']")).toBeTruthy();
    expect(container.querySelector("[role='alert']")).toBeNull();
  });
});

describe("dismissing", () => {
  it("closes on the button", () => {
    const onDismiss = vi.fn();
    render(<ExportBanner outcome={ok} onOpen={() => {}} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalled();
  });

  it("closes on Escape", () => {
    const onDismiss = vi.fn();
    render(<ExportBanner outcome={ok} onOpen={() => {}} onDismiss={onDismiss} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onDismiss).toHaveBeenCalled();
  });
});
