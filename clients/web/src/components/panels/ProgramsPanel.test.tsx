// @vitest-environment jsdom
/**
 * The programs page: the person lets a program answer approval requests.
 *
 * Pinned: a token is made only when asked, shown once with a way to
 * copy it, revoking clears it and says how many went, and a refusal
 * from the backend is shown rather than swallowed.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { ProgramsPanel } from "./ProgramsPanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function panel(answers: Record<string, unknown>) {
  const rpc = vi.fn(async (method: string) => {
    const answer = answers[method];
    if (answer instanceof Error) throw answer;
    return answer;
  });
  render(<ProgramsPanel client={{ rpc } as never} onClose={() => {}} />);
  return rpc;
}

describe("the programs page", () => {
  it("makes no token until the person asks for one", () => {
    const rpc = panel({});
    expect(rpc).not.toHaveBeenCalled();
    expect(screen.queryByText(/shown once/)).toBeNull();
  });

  it("shows a new token once, with a way to copy it", async () => {
    const writeText = vi.fn(async () => {});
    Object.assign(navigator, { clipboard: { writeText } });
    const rpc = panel({ mint_approver_token: { token: "tok-123" } });
    fireEvent.click(screen.getByText("Make an approver token"));
    expect(await screen.findByText("tok-123")).toBeTruthy();
    expect(rpc).toHaveBeenCalledWith("mint_approver_token");
    fireEvent.click(screen.getByText("Copy"));
    expect(await screen.findByText("Copied")).toBeTruthy();
    expect(writeText).toHaveBeenCalledWith("tok-123");
  });

  it("revokes every token and says how many went", async () => {
    panel({ mint_approver_token: { token: "tok-123" }, revoke_approver_tokens: { revoked: 2 } });
    fireEvent.click(screen.getByText("Make an approver token"));
    await screen.findByText("tok-123");
    fireEvent.click(screen.getByText("Revoke all approver tokens"));
    expect(await screen.findByText(/Revoked 2 approver tokens/)).toBeTruthy();
    expect(screen.queryByText("tok-123")).toBeNull();
  });

  it("says why when the backend refuses", async () => {
    panel({ mint_approver_token: new Error("mint_approver_token: only igni's window may do this") });
    fireEvent.click(screen.getByText("Make an approver token"));
    expect(await screen.findByText(/only igni's window may do this/)).toBeTruthy();
  });
});
