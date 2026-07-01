// @vitest-environment jsdom
/**
 * Component tests for the four status-line widgets:
 *
 *   • ModeBadge       — permission-mode chip (rows 50, 51, 7)
 *   • AutoApproveSwitch — one-click bypassPermissions toggle (row 51)
 *   • SessionChip     — short session-id with click-to-copy
 *   • CtxMeter        — context-usage bar with tonal grading
 *
 * Each widget has a small, sharp contract — wrong styling or wrong
 * callback wiring is immediately visible in the UI. These tests
 * lock the contracts so a "neat refactor" can't silently regress
 * them (e.g. swapping green/red, miscounting pct thresholds).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import {
  AutoApproveSwitch,
  CtxMeter,
  ModeBadge,
  SessionChip,
} from "./StatusBits";

afterEach(() => {
  cleanup();
});

// ── ModeBadge ────────────────────────────────────────────────

describe("ModeBadge", () => {
  it("renders nothing in default mode (signal-value: badge visible == mode active)", () => {
    // The whole point of the badge is "you only see it when you
    // need to" — rendering an empty / 'NORMAL' chip in default
    // mode would dilute that signal value.
    const { container } = render(<ModeBadge mode="default" />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing for an unknown mode (forward-compat guard)", () => {
    // If the BE ever pushes a new mode the FE doesn't know about,
    // render nothing rather than crashing or rendering a malformed
    // chip. Mirrors the same forward-compat strategy used in
    // ``normalizePlanTask``.
    const { container } = render(<ModeBadge mode="brandNewMode" />);
    expect(container.firstChild).toBeNull();
  });

  it("plan mode shows 'PLAN MODE' label", () => {
    render(<ModeBadge mode="plan" />);
    expect(screen.getByText("PLAN MODE")).toBeTruthy();
  });

  it("acceptEdits mode shows 'ACCEPT EDITS' label + accept variant class", () => {
    const { container } = render(<ModeBadge mode="acceptEdits" />);
    expect(screen.getByText("ACCEPT EDITS")).toBeTruthy();
    // CSS variant class drives the green styling — refactor to
    // a different naming convention would silently break the
    // visual distinction between modes.
    expect(container.querySelector(".mode-badge--accept")).toBeTruthy();
  });

  it("dontAsk mode shows 'STRICT' label + dontask variant class", () => {
    const { container } = render(<ModeBadge mode="dontAsk" />);
    expect(screen.getByText(/STRICT/)).toBeTruthy();
    expect(container.querySelector(".mode-badge--dontask")).toBeTruthy();
  });

  it("bypassPermissions mode shows the loud 'BYPASS PERMISSIONS' label", () => {
    // This mode is the sharpest one — agent can do anything
    // without prompting. The label has to be unmistakeable
    // (full caps, no abbreviation) so users don't leave it on
    // by accident.
    const { container } = render(<ModeBadge mode="bypassPermissions" />);
    expect(screen.getByText("BYPASS PERMISSIONS")).toBeTruthy();
    expect(container.querySelector(".mode-badge--bypass")).toBeTruthy();
  });

  it("includes a hover title with explanation copy", () => {
    // The chip is small — the title attr is where the full
    // "what does this mode mean" copy lives. Without it the
    // user has to leave the app to look up what they enabled.
    const { container } = render(<ModeBadge mode="plan" />);
    const chip = container.querySelector(".plan-badge");
    expect(chip?.getAttribute("title")).toMatch(/plan mode/i);
    expect(chip?.getAttribute("title")).toMatch(/\/plan/);
  });
});

// ── AutoApproveSwitch ────────────────────────────────────────

describe("AutoApproveSwitch", () => {
  it("renders OFF when mode is not bypassPermissions", () => {
    // ``aria-checked=false`` for SR users + no ``is-on`` class
    // for visual. Label says "Auto-approve" with no suffix in
    // the OFF state to keep the chip compact.
    const { container } = render(
      <AutoApproveSwitch mode="default" onToggle={() => undefined} />,
    );
    const button = container.querySelector("button[role='switch']");
    expect(button?.getAttribute("aria-checked")).toBe("false");
    expect(button?.className).not.toContain("is-on");
    expect(screen.getByText("Auto-approve")).toBeTruthy();
  });

  it("renders ON when mode === bypassPermissions", () => {
    const { container } = render(
      <AutoApproveSwitch mode="bypassPermissions" onToggle={() => undefined} />,
    );
    const button = container.querySelector("button[role='switch']");
    expect(button?.getAttribute("aria-checked")).toBe("true");
    expect(button?.className).toContain("is-on");
    // Label gains an "ON" suffix so the switch reads correctly
    // even without color. Accessibility-by-text-too.
    expect(screen.getByText(/Auto-approve ON/)).toBeTruthy();
  });

  it("clicking OFF→ON calls onToggle(true)", () => {
    const onToggle = vi.fn();
    const { container } = render(
      <AutoApproveSwitch mode="default" onToggle={onToggle} />,
    );
    fireEvent.click(container.querySelector("button[role='switch']")!);
    expect(onToggle).toHaveBeenCalledWith(true);
  });

  it("clicking ON→OFF calls onToggle(false)", () => {
    const onToggle = vi.fn();
    const { container } = render(
      <AutoApproveSwitch mode="bypassPermissions" onToggle={onToggle} />,
    );
    fireEvent.click(container.querySelector("button[role='switch']")!);
    expect(onToggle).toHaveBeenCalledWith(false);
  });

  it("OFF title hints what turning ON does", () => {
    // The title flips with state so the affordance is always
    // forward-looking: "click to turn ON" vs "click to turn OFF".
    const { container } = render(
      <AutoApproveSwitch mode="default" onToggle={() => undefined} />,
    );
    const title = container
      .querySelector("button[role='switch']")
      ?.getAttribute("title");
    expect(title).toMatch(/click to let|without prompts/i);
  });

  it("ON title hints what turning OFF does", () => {
    const { container } = render(
      <AutoApproveSwitch mode="bypassPermissions" onToggle={() => undefined} />,
    );
    const title = container
      .querySelector("button[role='switch']")
      ?.getAttribute("title");
    expect(title).toMatch(/click to turn off/i);
  });
});

// ── SessionChip ──────────────────────────────────────────────

describe("SessionChip", () => {
  it("renders the placeholder 'session —' for an empty id", () => {
    // No live session yet. Showing a clickable empty pill would
    // be confusing; render an inert dash so the slot still has
    // shape but nothing happens on click.
    render(<SessionChip sessionId="" />);
    expect(screen.getByText("session —")).toBeTruthy();
  });

  it("renders the first 8 chars of the session id", () => {
    // The 8-char prefix is the canonical short form used
    // everywhere else in the codebase (Session.fork, persistence,
    // logs). Drift here would break copy/paste round-tripping
    // with logs.
    const { container } = render(<SessionChip sessionId="abcd1234ef56789" />);
    const code = container.querySelector("code");
    expect(code?.textContent).toBe("abcd1234");
  });

  it("title attr shows what the click does (copy full id)", () => {
    // The visible label is the short prefix — the full id is
    // only discoverable via the title. Without that, the user
    // has no way to know they can grab the full string.
    const { container } = render(<SessionChip sessionId="abcd1234ef56789" />);
    const btn = container.querySelector("button");
    expect(btn?.getAttribute("title")).toContain("abcd1234ef56789");
  });

  it("click copies the FULL id to clipboard (not the short prefix)", async () => {
    // The button shows ``abcd1234`` but copy must yield the full
    // session id — that's the whole reason for the chip's
    // existence (clipboard handoff to ``ember-code resume <id>``
    // or to logs that ship 32-char ids).
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const { container } = render(<SessionChip sessionId="abcd1234ef56789" />);
    fireEvent.click(container.querySelector("button")!);
    // Tick microtasks so the async copy + state update flush.
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith("abcd1234ef56789");
  });
});

// ── CtxMeter ─────────────────────────────────────────────────

describe("CtxMeter", () => {
  it("ok tone for pct < 60", () => {
    // The thresholds are not arbitrary — the user starts
    // worrying at "more than half used" (warn) and panicking
    // at "running out" (danger). Pin both transitions so a
    // 5% drift doesn't silently change the affordance.
    const { container } = render(<CtxMeter tokens={1000} max={10000} pct={20} />);
    expect(container.querySelector(".tone-ok")).toBeTruthy();
    expect(container.querySelector(".tone-warn")).toBeNull();
    expect(container.querySelector(".tone-danger")).toBeNull();
  });

  it("warn tone at exactly 60", () => {
    // 60 is the transition point. Off-by-one in the source
    // (>= vs >) would flip this; pin both boundaries.
    const { container } = render(<CtxMeter tokens={6000} max={10000} pct={60} />);
    expect(container.querySelector(".tone-warn")).toBeTruthy();
  });

  it("danger tone at exactly 85", () => {
    const { container } = render(<CtxMeter tokens={8500} max={10000} pct={85} />);
    expect(container.querySelector(".tone-danger")).toBeTruthy();
  });

  it("clamps pct above 100 to 100% fill width", () => {
    // The pct comes from `tokens / max` which can momentarily
    // exceed 100 if the BE's token-count lags the conversation
    // size. Filling >100% would overflow the track visually.
    const { container } = render(<CtxMeter tokens={120_000} max={100_000} pct={120} />);
    const fill = container.querySelector(".ctx-meter-fill") as HTMLElement | null;
    expect(fill?.style.width).toBe("100%");
  });

  it("clamps negative pct to 0", () => {
    // Defensive: a stale or buggy upstream could send a
    // negative pct. Show an empty bar rather than a glitched
    // one.
    const { container } = render(<CtxMeter tokens={0} max={10000} pct={-5} />);
    const fill = container.querySelector(".ctx-meter-fill") as HTMLElement | null;
    expect(fill?.style.width).toBe("0%");
  });

  it("formats tokens under 1000 verbatim", () => {
    render(<CtxMeter tokens={999} max={10000} pct={9.99} />);
    expect(screen.getByText(/999/)).toBeTruthy();
  });

  it("formats tokens 1000-9999 with one decimal k", () => {
    // ``1.5k`` reads quicker than ``1500`` for a status-line
    // metric where the user just wants a magnitude.
    render(<CtxMeter tokens={1500} max={10000} pct={15} />);
    expect(screen.getByText(/1\.5k/)).toBeTruthy();
  });

  it("formats tokens >=10000 with no decimal", () => {
    // Past 10k a decimal is noise — pin to integer form so
    // ``42k`` not ``42.0k``.
    render(<CtxMeter tokens={42_000} max={100_000} pct={42} />);
    expect(screen.getByText(/42k/)).toBeTruthy();
    expect(screen.queryByText(/42\.0k/)).toBeNull();
  });

  it("title attr includes the raw token count + max for hover precision", () => {
    // Status-line numbers are coarse for at-a-glance reading;
    // the tooltip is where the exact numbers live for users
    // who want to verify.
    const { container } = render(<CtxMeter tokens={42_000} max={100_000} pct={42} />);
    const title = container.querySelector(".ctx-meter")?.getAttribute("title");
    expect(title).toContain("42,000");
    expect(title).toContain("100,000");
  });

  it("title attr omits max when max is 0 (no model context window known)", () => {
    // ``max=0`` shows up briefly during /clear → before the
    // status update lands. Don't say "0 of 0 tokens" — show
    // just the absolute count.
    const { container } = render(<CtxMeter tokens={1234} max={0} pct={0} />);
    const title = container.querySelector(".ctx-meter")?.getAttribute("title");
    expect(title).toContain("1,234");
    expect(title).not.toContain(" of 0");
  });
});
