// @vitest-environment jsdom
/**
 * The hooks page: which lifecycle events run what, read-only.
 *
 * Pinned: the backend's duplicates (the same hook from a project file
 * and a plugin) show once, events come in lifecycle order with unknown
 * ones after, a script opens through the host bridge while a webhook
 * does not pretend to be a file, and a failed call says why.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { HooksPanel } from "./HooksPanel";
import { host } from "../../lib/host";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function hook(over: Record<string, unknown> = {}) {
  return {
    event: "PreToolUse",
    type: "command",
    command: ".igni/hooks/guard.sh",
    url: "",
    matcher: "",
    timeout_ms: 10000,
    background: false,
    headers: {},
    ...over,
  };
}

function panelWith(answer: unknown) {
  const rpc = vi.fn(async () => {
    if (answer instanceof Error) throw answer;
    return answer;
  });
  render(<HooksPanel client={{ rpc } as never} onClose={() => {}} />);
  return rpc;
}

describe("the hooks page", () => {
  it("says how to add one when there are none", async () => {
    panelWith([]);
    expect(await screen.findByText("No hooks configured")).toBeTruthy();
    expect(screen.getByText("0 hooks")).toBeTruthy();
  });

  it("treats a null or non-list answer as no hooks", async () => {
    panelWith(null);
    expect(await screen.findByText("No hooks configured")).toBeTruthy();
    cleanup();
    panelWith({ unexpected: true });
    expect(await screen.findByText("No hooks configured")).toBeTruthy();
  });

  it("says why when the backend call fails", async () => {
    panelWith(new Error("get_hooks_details not found"));
    expect(await screen.findByText("get_hooks_details not found")).toBeTruthy();
    expect(screen.getByText("No hooks configured")).toBeTruthy();
  });

  it("shows a hook defined twice only once", async () => {
    panelWith([hook(), hook(), hook({ matcher: "Bash" })]);
    expect(await screen.findByText("2 hooks")).toBeTruthy();
    expect(screen.getAllByText(".igni/hooks/guard.sh")).toHaveLength(2);
  });

  it("orders events by lifecycle, then unknown ones alphabetically", async () => {
    panelWith([
      hook({ event: "ZetaEvent", command: "z.sh" }),
      hook({ event: "Stop", command: "stop.sh" }),
      hook({ event: "AlphaEvent", command: "a.sh" }),
      hook({ event: "SessionStart", command: "start.sh" }),
    ]);
    await screen.findByText("4 hooks");
    const events = [...document.querySelectorAll(".hooks-event-name")].map((e) => e.textContent);
    expect(events).toEqual(["SessionStart", "Stop", "AlphaEvent", "ZetaEvent"]);
    // Known events explain themselves; unknown ones carry no blurb.
    expect(screen.getByText(/Runs when a session boots/)).toBeTruthy();
    expect(document.querySelectorAll(".hooks-event-blurb")).toHaveLength(2);
  });

  it("tags the matcher, background and a non-default timeout", async () => {
    panelWith([
      hook({ event: "PostToolUse", matcher: "Edit|Write", background: true, timeout_ms: 30000 }),
    ]);
    expect(await screen.findByText("1 hook")).toBeTruthy();
    expect(screen.getByText("matches: Edit|Write")).toBeTruthy();
    expect(screen.getByText("background")).toBeTruthy();
    expect(screen.getByText("30s timeout")).toBeTruthy();
  });

  it("opens a script through the host", async () => {
    const open = vi.spyOn(host, "openFile").mockResolvedValue(true);
    panelWith([hook()]);
    fireEvent.click(await screen.findByRole("button", { name: /guard\.sh/ }));
    expect(open).toHaveBeenCalledWith(".igni/hooks/guard.sh");
  });

  it("does not offer to open a webhook as a file", async () => {
    panelWith([
      hook({ type: "webhook", command: "", url: "https://hooks.acme.test/igni" }),
      hook({ type: "webhook", command: "", url: "", event: "Stop" }),
      hook({ command: "", event: "Notification" }),
    ]);
    expect(await screen.findByText("https://hooks.acme.test/igni")).toBeTruthy();
    expect(screen.getByText("(no url)")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /hooks\.acme\.test/ })).toBeNull();
    // A command hook with no command still renders, named as such.
    expect(screen.getByRole("button", { name: /\(no command\)/ })).toBeTruthy();
  });
});
