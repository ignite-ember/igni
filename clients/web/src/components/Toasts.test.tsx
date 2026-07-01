// @vitest-environment jsdom
/**
 * Tests for ``Toasts`` — the top-right notification stack used for
 * project-level async events (scheduled tasks finishing, etc.).
 *
 * Critical contracts:
 *   • Auto-dismiss fires after ``ttlMs`` (default 8000) + a short
 *     animation tail. Without this, toasts accumulate forever.
 *   • Click body → fires onClick AND dismisses (it's a "primary
 *     action" affordance, e.g. "switch to that conversation").
 *   • Close X → dismisses ONLY (does NOT fire onClick, otherwise
 *     the user can't dismiss without taking the action).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Toasts, type Toast } from "./Toasts";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Toasts — list rendering", () => {
  it("renders nothing when items is empty", () => {
    // The empty wrapper would still take a row in flex / grid
    // layouts even if invisible; collapse cleanly.
    const { container } = render(<Toasts items={[]} onDismiss={() => undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders one card per toast with title + body", () => {
    const items: Toast[] = [
      { id: 1, title: "First", body: "body one" },
      { id: 2, title: "Second", body: "body two" },
    ];
    render(<Toasts items={items} onDismiss={() => undefined} />);
    expect(screen.getByText("First")).toBeTruthy();
    expect(screen.getByText("body one")).toBeTruthy();
    expect(screen.getByText("Second")).toBeTruthy();
    expect(screen.getByText("body two")).toBeTruthy();
  });

  it("body is optional — only the title row renders when body is absent", () => {
    const { container } = render(
      <Toasts items={[{ id: 1, title: "Just a title" }]} onDismiss={() => undefined} />,
    );
    expect(screen.getByText("Just a title")).toBeTruthy();
    // No ``toast-body`` element when ``body`` isn't passed.
    expect(container.querySelector(".toast-body")).toBeNull();
  });

  it("wrapper has the right ARIA landmark", () => {
    // Screen readers announce toasts; the region landmark
    // tells them where notifications live. Aria attrs are
    // small but easy to silently drop.
    const { container } = render(
      <Toasts items={[{ id: 1, title: "x" }]} onDismiss={() => undefined} />,
    );
    const region = container.querySelector(".toasts");
    expect(region?.getAttribute("role")).toBe("region");
    expect(region?.getAttribute("aria-label")).toBe("Notifications");
  });
});

describe("Toasts — body button (primary action)", () => {
  it("clicking the body fires onClick", () => {
    // The body click is the toast's "do the thing" action —
    // e.g. open the conversation a scheduled task landed in.
    const onClick = vi.fn();
    const onDismiss = vi.fn();
    render(
      <Toasts
        items={[{ id: 7, title: "click me", onClick }]}
        onDismiss={onDismiss}
      />,
    );
    fireEvent.click(screen.getByText("click me"));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("clicking the body then dismisses (animation tail then onDismiss)", () => {
    // Click → start exit animation → 150ms later, drop from
    // the list. The 150ms tail keeps the animation visible.
    vi.useFakeTimers();
    const onClick = vi.fn();
    const onDismiss = vi.fn();
    render(
      <Toasts
        items={[{ id: 7, title: "click me", onClick }]}
        onDismiss={onDismiss}
      />,
    );
    fireEvent.click(screen.getByText("click me"));
    // Synchronously, onClick fired but onDismiss hasn't yet —
    // the animation hasn't completed.
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onDismiss).not.toHaveBeenCalled();
    // Advance past the 150ms exit-animation tail.
    act(() => {
      vi.advanceTimersByTime(150);
    });
    expect(onDismiss).toHaveBeenCalledWith(7);
  });

  it("clicking the body without an onClick still dismisses (no crash)", () => {
    // The handler is optional — informational toasts without an
    // action still close on click. The ``?.()`` chain in the
    // source guards this.
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(
      <Toasts items={[{ id: 7, title: "info only" }]} onDismiss={onDismiss} />,
    );
    fireEvent.click(screen.getByText("info only"));
    act(() => {
      vi.advanceTimersByTime(150);
    });
    expect(onDismiss).toHaveBeenCalledWith(7);
  });

  it("applies the is-closing class while the exit animation plays", () => {
    // Without the class, the CSS animation can't fire — the
    // toast would just blink out. Lock the class transition
    // on body-click so a styling refactor doesn't silently
    // skip the animation.
    vi.useFakeTimers();
    const { container } = render(
      <Toasts items={[{ id: 7, title: "click me" }]} onDismiss={() => undefined} />,
    );
    expect(container.querySelector(".toast")?.className).not.toContain(
      "is-closing",
    );
    fireEvent.click(screen.getByText("click me"));
    // Class flipped synchronously, before the animation tail.
    expect(container.querySelector(".toast")?.className).toContain("is-closing");
  });
});

describe("Toasts — dismiss (X) button", () => {
  it("dismisses without firing onClick", () => {
    // The most subtle bug here: an event bubbling from the X
    // button up to the body button would fire onClick when
    // the user only meant to close. The source uses
    // ``stopPropagation`` to guard against this; locked here.
    vi.useFakeTimers();
    const onClick = vi.fn();
    const onDismiss = vi.fn();
    render(
      <Toasts
        items={[{ id: 7, title: "x", onClick }]}
        onDismiss={onDismiss}
      />,
    );
    fireEvent.click(screen.getByLabelText("Dismiss"));
    expect(onClick).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(150);
    });
    expect(onDismiss).toHaveBeenCalledWith(7);
  });
});

describe("Toasts — auto-dismiss TTL", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("auto-dismisses after default 8000ms + 200ms animation tail", () => {
    // The default-TTL contract — silent acknowledgement that
    // most toasts shouldn't need a manual close. 8s gives a
    // user enough time to read but doesn't pile up notifications.
    const onDismiss = vi.fn();
    render(<Toasts items={[{ id: 7, title: "x" }]} onDismiss={onDismiss} />);
    // Just before TTL — still mounted, not closing.
    act(() => {
      vi.advanceTimersByTime(7999);
    });
    expect(onDismiss).not.toHaveBeenCalled();
    // TTL hits → ``is-closing`` flips → 200ms tail fires onDismiss.
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(onDismiss).toHaveBeenCalledWith(7);
  });

  it("custom ttlMs is respected", () => {
    // Some flows want a long-lived toast (e.g. "update
    // available" until the user clicks). The override exists
    // for that — make sure 30s actually stays for 30s.
    const onDismiss = vi.fn();
    render(
      <Toasts
        items={[{ id: 7, title: "long", ttlMs: 30_000 }]}
        onDismiss={onDismiss}
      />,
    );
    // Default-TTL would have fired by 8000+200 = 8200; the
    // long one is still alive.
    act(() => {
      vi.advanceTimersByTime(8200);
    });
    expect(onDismiss).not.toHaveBeenCalled();
    // Past 30s + 200ms tail.
    act(() => {
      vi.advanceTimersByTime(30_000 - 8200 + 200);
    });
    expect(onDismiss).toHaveBeenCalledWith(7);
  });
});
