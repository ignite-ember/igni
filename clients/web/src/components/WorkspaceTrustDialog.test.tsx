// @vitest-environment jsdom
/**
 * "Trust this folder?" — the window's half of workspace trust.
 *
 * The three answers reach the backend as `set_workspace_trust`; a policy
 * refusal shows its reason and offers only OK (no way to trust); a failed
 * answer says why and leaves the dialog open.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { WorkspaceTrustDialog } from "./WorkspaceTrustDialog";
import {
  isTrustStatus,
  trustFromPush,
  type WorkspaceTrustStatus,
} from "../workspaceTrust/model";

const asking: WorkspaceTrustStatus = {
  state: "ask",
  can_trust: true,
  reason: "You have not decided whether to trust this folder yet.",
  path: "/work/app",
  remote: "github.com/acme/app",
  policy_mode: "ask",
  gated: "project tools, hooks, workflows",
};

afterEach(cleanup);

function rpcStub(result: unknown = asking) {
  return vi.fn(async (_method: string, _args?: unknown) => result) as unknown as (
    method: string,
    args?: Record<string, unknown>,
  ) => Promise<never>;
}

describe("WorkspaceTrustDialog", () => {
  it("shows the folder and its git remote", () => {
    render(<WorkspaceTrustDialog client={{ rpc: rpcStub() }} status={asking} onClose={() => {}} />);
    expect(screen.getByText("Trust this folder?")).toBeTruthy();
    expect(screen.getByText("/work/app")).toBeTruthy();
    expect(screen.getByText("github.com/acme/app")).toBeTruthy();
  });

  it.each([
    ["Trust", "trust"],
    ["Don't trust", "distrust"],
    ["This time only", "once"],
  ])("%s answers %s", async (label, decision) => {
    const rpc = rpcStub();
    const onClose = vi.fn();
    render(<WorkspaceTrustDialog client={{ rpc }} status={asking} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: label }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(rpc).toHaveBeenCalledWith("set_workspace_trust", { decision });
  });

  it("explains a policy refusal and offers no way to trust", () => {
    const refused = {
      ...asking,
      state: "untrusted" as const,
      can_trust: false,
      reason: "Your organisation only allows folders from approved git remotes.",
    };
    render(<WorkspaceTrustDialog client={{ rpc: rpcStub() }} status={refused} onClose={() => {}} />);
    expect(screen.getByText(refused.reason)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Trust" })).toBeNull();
    expect(screen.getByRole("button", { name: "OK" })).toBeTruthy();
  });

  it("keeps the dialog open and says why when the answer fails", async () => {
    const rpc = vi.fn(async () => {
      throw new Error("refused by policy");
    }) as unknown as (method: string, args?: Record<string, unknown>) => Promise<never>;
    const onClose = vi.fn();
    render(<WorkspaceTrustDialog client={{ rpc }} status={asking} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Trust" }));
    await waitFor(() => expect(screen.getByText("refused by policy")).toBeTruthy());
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("workspace trust push", () => {
  it("reads the status off its channel and nothing else", () => {
    expect(trustFromPush({ channel: "workspace_trust_required", payload: asking })).toEqual(asking);
    expect(trustFromPush({ channel: "notifications_changed", payload: asking })).toBeNull();
    expect(isTrustStatus({ state: "maybe", can_trust: true, path: "/x" })).toBe(false);
  });
});
