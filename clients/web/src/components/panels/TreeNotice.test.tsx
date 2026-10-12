// @vitest-environment jsdom
/**
 * What just happened, said over the page instead of inside a column.
 *
 * The sentence "Archived. It is out of the tree; anything built on it
 * still resolves." used to render as a paragraph at the top of the
 * hierarchy, pushing every folder down two lines to announce that one
 * of them had gone. The column is for the tree.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TreeNotice } from "./TreeNotice";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const ARCHIVED = "Archived. It is out of the tree; anything built on it still resolves.";

describe("saying what happened", () => {
  it("renders nothing at all when there is nothing to say", () => {
    const { container } = render(<TreeNotice text="" onDismiss={() => {}} />);

    expect(container.firstChild).toBeNull();
    expect(document.querySelector(".tree-notice")).toBeNull();
  });

  it("puts the notice on the page, not inside the caller", () => {
    // Portalled: the dock it is raised from is a fixed-width column,
    // and a notice clipped to that column is the narrowest possible
    // place to read a sentence.
    const { container } = render(<TreeNotice text={ARCHIVED} onDismiss={() => {}} />);

    expect(container.querySelector(".tree-notice")).toBeNull();
    expect(document.body.querySelector(".tree-notice")).toBeTruthy();
  });

  it("says it out loud for a screen reader", () => {
    render(<TreeNotice text={ARCHIVED} onDismiss={() => {}} />);

    expect(screen.getByRole("status").textContent).toContain("Archived.");
  });

  it("can be put down by hand", () => {
    const dismissed = vi.fn();
    render(<TreeNotice text={ARCHIVED} onDismiss={dismissed} />);

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(dismissed).toHaveBeenCalled();
  });
});

describe("how long it stays", () => {
  it("a result goes away on its own", () => {
    vi.useFakeTimers();
    const dismissed = vi.fn();
    render(<TreeNotice text={ARCHIVED} onDismiss={dismissed} />);

    vi.advanceTimersByTime(6000);

    expect(dismissed).toHaveBeenCalled();
  });

  it("a refusal does not", () => {
    // It needs reading. A banner that vanishes while somebody is
    // still working out what it said is worse than no banner.
    vi.useFakeTimers();
    const dismissed = vi.fn();
    render(
      <TreeNotice text="Three documents depend on this." tone="refused" onDismiss={dismissed} />,
    );

    vi.advanceTimersByTime(60_000);

    expect(dismissed).not.toHaveBeenCalled();
  });

  it("a second result gets its own time, not what was left of the first", () => {
    vi.useFakeTimers();
    const dismissed = vi.fn();
    const { rerender } = render(<TreeNotice text="Renamed." onDismiss={dismissed} />);
    vi.advanceTimersByTime(5000);

    rerender(<TreeNotice text={ARCHIVED} onDismiss={dismissed} />);
    vi.advanceTimersByTime(2000);

    // The first timer would have fired by now had it survived.
    expect(dismissed).not.toHaveBeenCalled();
    vi.advanceTimersByTime(4000);
    expect(dismissed).toHaveBeenCalled();
  });
});
