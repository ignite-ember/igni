// @vitest-environment jsdom
/**
 * An extension's chip in igni's own chrome.
 *
 * Added because the *first* extension needed one: its chip sat in the
 * composer, hardcoded, and when it became an extension that chip had
 * to come from its manifest or be lost.
 * `indicators` had been moved to "not in v1" on the grounds that
 * nothing specified it — right about the specification, wrong about
 * the need.
 *
 * Most of what is tested here is refusal, because the risk is an
 * extension putting something into the host's chrome that the host
 * did not agree to draw.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ExtensionIndicator, isIndicatorState } from "./ExtensionIndicator";

afterEach(cleanup);

const INDICATOR = {
  id: "acme.scanner.freshness",
  label: "Scanner",
  source: "indicator",
  opens: "acme.scanner",
  intervalSeconds: 30,
};

function draw(answer: unknown, overrides = {}) {
  const onCall = vi.fn().mockResolvedValue(answer);
  const onOpen = vi.fn();
  const view = render(
    <ExtensionIndicator
      extensionId="acme.scanner"
      indicator={{ ...INDICATOR, ...overrides }}
      onCall={onCall}
      onOpen={onOpen}
    />,
  );
  return { ...view, onCall, onOpen };
}

describe("what it draws", () => {
  it("shows what the extension said", async () => {
    draw({ label: "fresh", tone: "success", detail: "HEAD indexed" });
    expect(await screen.findByText("fresh")).not.toBeNull();
  });

  it("asks the extension's own namespace", async () => {
    const { onCall } = draw({ label: "fresh", tone: "success", detail: "" });
    await screen.findByText("fresh");
    expect(onCall).toHaveBeenCalledWith("ext.acme.scanner.indicator", {});
  });

  it("maps a tone to one of the four states the floor publishes", async () => {
    const { container } = draw({ label: "stale", tone: "warning", detail: "" });
    await screen.findByText("stale");
    expect(
      container.querySelector(".ui-status")?.getAttribute("data-state"),
    ).toBe("PREPARING");
  });
});

describe("what it refuses to draw", () => {
  it("draws nothing for a tone igni does not have", async () => {
    // `tone` picks a colour. A value outside the four would be a
    // chip in a colour that is not in the design system, put there
    // by somebody else's typo.
    const { container } = draw({ label: "fine", tone: "chartreuse", detail: "" });
    await act(async () => {});
    expect(container.querySelector(".ui-status")).toBeNull();
  });

  it("draws nothing for an answer that is not an indicator state", async () => {
    const { container } = draw("fresh");
    await act(async () => {});
    expect(container.querySelector(".ui-status")).toBeNull();
  });

  it("draws nothing when the call fails", async () => {
    // The extension's own FAILED state already carries a reason in
    // the list. Two places saying the same thing differently is
    // worse than one.
    const onCall = vi.fn().mockRejectedValue(new Error("refused"));
    const { container } = render(
      <ExtensionIndicator
        extensionId="acme.scanner"
        indicator={INDICATOR}
        onCall={onCall}
      />,
    );
    await act(async () => {});
    expect(container.querySelector(".ui-status")).toBeNull();
  });
});

describe("the guard itself", () => {
  it.each([
    [{ label: "a", tone: "success", detail: "" }, true],
    [{ label: "a", tone: "neutral", detail: "" }, true],
    [{ label: "a", tone: "nope", detail: "" }, false],
    [{ tone: "success", detail: "" }, false],
    [{ label: 7, tone: "success" }, false],
    ["fresh", false],
    [null, false],
    [[], false],
  ])("%o -> %s", (value, expected) => {
    expect(isIndicatorState(value)).toBe(expected);
  });
});

describe("clicking", () => {
  it("opens the page the manifest named", async () => {
    const { onOpen } = draw({ label: "stale", tone: "warning", detail: "" });
    fireEvent.click(await screen.findByRole("button"));
    expect(onOpen).toHaveBeenCalledWith("acme.scanner");
  });

  it("says what clicking does, not just what the state is", async () => {
    // A button whose whole accessible name is "stale" is a button
    // nobody can tell is a button.
    draw({ label: "stale", tone: "warning", detail: "" });
    expect(
      (await screen.findByRole("button")).getAttribute("aria-label"),
    ).toBe("Scanner: stale. Open.");
  });

  it("is not a button when the manifest named no page", async () => {
    const { container } = draw(
      { label: "fresh", tone: "success", detail: "" },
      { opens: "" },
    );
    await screen.findByText("fresh");
    expect(container.querySelector("button")).toBeNull();
  });
});

describe("polling", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("will not poll faster than the host's floor", async () => {
    // An extension asking to be polled ten times a second is asking
    // for igni's main thread.
    const onCall = vi.fn().mockResolvedValue({
      label: "fresh",
      tone: "success",
      detail: "",
    });
    render(
      <ExtensionIndicator
        extensionId="acme.scanner"
        indicator={{ ...INDICATOR, intervalSeconds: 0.1 }}
        onCall={onCall}
      />,
    );
    await act(async () => {});
    const afterFirst = onCall.mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(1_000);
    });
    expect(onCall.mock.calls.length).toBe(afterFirst);
  });
});

describe("the floor's unread count on the chip", () => {
  // The extension no longer computes a "waiting" badge. The count is
  // the floor's — igni server's and this machine's together — and the
  // host draws it beside whatever the extension said.
  it("is drawn beside the extension's own state", async () => {
    const onCall = vi.fn().mockResolvedValue({ label: "fresh", tone: "success", detail: "" });
    render(
      <ExtensionIndicator
        extensionId="acme.scanner"
        indicator={INDICATOR}
        onCall={onCall}
        onOpen={() => {}}
        unread={4}
      />,
    );

    expect(await screen.findByText("4")).not.toBeNull();
    expect(screen.getByRole("button", { name: /4 unread/ })).not.toBeNull();
  });

  it("does not wait for the extension to answer", async () => {
    const onCall = vi.fn().mockRejectedValue(new Error("not started"));
    const onOpen = vi.fn();
    render(
      <ExtensionIndicator
        extensionId="acme.scanner"
        indicator={INDICATOR}
        onCall={onCall}
        onOpen={onOpen}
        unread={2}
      />,
    );

    const button = await screen.findByRole("button", { name: /2 unread/ });
    fireEvent.click(button);
    expect(onOpen).toHaveBeenCalledWith("acme.scanner");
  });

  it("draws nothing for nothing unread", async () => {
    const { container } = draw({ label: "fresh", tone: "success", detail: "" });
    await screen.findByText("fresh");

    expect(container.querySelector(".extension-badge")).toBeNull();
  });
});
