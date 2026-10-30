// @vitest-environment jsdom
/**
 * What an icon means, on hover and on focus.
 *
 * A toolbar of glyphs is quicker to scan and slower to learn. The
 * browser's own ``title`` does technically say the word — after a
 * second and a half, in the operating system's font, at the pointer
 * rather than at the control, and never at all for somebody on a
 * keyboard.
 *
 * The dwell is the part worth pinning: a pointer crossing a row of
 * buttons on its way somewhere else must not leave a trail of
 * popups, while a keyboard user arriving at a control has asked for
 * the word by arriving.
 */
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Tooltip } from "./Tooltip";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function iconButton(text = "Show archived documents") {
  return render(
    <Tooltip text={text}>
      <button type="button" aria-label="Show archived documents">
        ▣
      </button>
    </Tooltip>,
  );
}

/**
 * The control, not the wrapper.
 *
 * The wrapper is ``display: contents`` and so has no box, which
 * means a real browser never hits it with a pointer. Firing at it
 * here passed for exactly that reason — jsdom does no hit-testing —
 * while the tooltip never once appeared on screen. Hovering what a
 * person hovers is the only version of this test worth having.
 */
const control = () => screen.getByRole("button");
const tip = () => document.querySelector(".tip");
const anchor = () => document.querySelector(".tip-anchor") as HTMLElement;

describe("hovering an icon", () => {
  it("says nothing at first", () => {
    iconButton();
    fireEvent.pointerEnter(control());

    // Still within the dwell: a pointer on its way past has not
    // asked for anything.
    expect(tip()).toBeNull();
  });

  it("says the word once the pointer rests", () => {
    vi.useFakeTimers();
    iconButton();

    fireEvent.pointerEnter(control());
    act(() => void vi.advanceTimersByTime(400));

    expect(tip()?.textContent).toBe("Show archived documents");
  });

  it("takes it away again on the way out", () => {
    vi.useFakeTimers();
    iconButton();
    fireEvent.pointerEnter(control());
    act(() => void vi.advanceTimersByTime(400));

    fireEvent.pointerLeave(control());

    expect(tip()).toBeNull();
  });

  it("does not appear at all if the pointer leaves before it settles", () => {
    vi.useFakeTimers();
    iconButton();

    fireEvent.pointerEnter(control());
    act(() => void vi.advanceTimersByTime(200));
    fireEvent.pointerLeave(control());
    act(() => void vi.advanceTimersByTime(400));

    expect(tip()).toBeNull();
  });

  it("gets out of the way when the control is pressed", () => {
    // The word has been read by then, and the thing it explains is
    // about to happen.
    vi.useFakeTimers();
    iconButton();
    fireEvent.pointerEnter(control());
    act(() => void vi.advanceTimersByTime(400));

    fireEvent.pointerDown(control());

    expect(tip()).toBeNull();
  });
});

describe("arriving by keyboard", () => {
  it("says the word straight away", () => {
    // No dwell. Somebody who tabbed to a control has asked for it by
    // arriving, and there is no pointer to rest.
    iconButton();

    fireEvent.focus(screen.getByRole("button"));

    expect(tip()?.textContent).toBe("Show archived documents");
  });

  it("takes it away on the way to the next control", () => {
    iconButton();
    fireEvent.focus(screen.getByRole("button"));

    fireEvent.blur(screen.getByRole("button"));

    expect(tip()).toBeNull();
  });

  it("closes on Escape without leaving the control", () => {
    iconButton();
    fireEvent.focus(screen.getByRole("button"));

    fireEvent.keyDown(window, { key: "Escape" });

    expect(tip()).toBeNull();
  });
});

describe("what it is, to a screen reader", () => {
  it("describes the control rather than announcing itself", () => {
    iconButton();
    fireEvent.focus(screen.getByRole("button"));

    expect(tip()?.getAttribute("role")).toBe("tooltip");
    expect(anchor().getAttribute("aria-describedby")).toBe(tip()?.id);
  });

  it("leaves the control's own label alone", () => {
    // The tooltip is the explanation, not the name. A screen reader
    // must still hear "Show archived documents" as the button.
    iconButton();

    expect(
      screen.getByRole("button", { name: "Show archived documents" }),
    ).toBeTruthy();
  });
});

describe("where it goes", () => {
  it("is rendered on the page, not inside the toolbar", () => {
    // Portalled: a toolbar is a fixed-width row, often inside a
    // column with its own clipping, and a tooltip trapped in one is
    // a tooltip nobody can read.
    const { container } = iconButton();
    fireEvent.focus(screen.getByRole("button"));

    expect(container.querySelector(".tip")).toBeNull();
    expect(document.body.querySelector(".tip")).toBeTruthy();
  });

  it("renders nothing when there is no word to say", () => {
    iconButton("");
    fireEvent.focus(screen.getByRole("button"));

    expect(tip()).toBeNull();
  });
});
