// @vitest-environment jsdom
/**
 * When the window asks "Trust this folder?" and when it stops asking.
 *
 * The dialog itself is `WorkspaceTrustDialog.test.tsx`. This is the hook
 * that decides it opens: on connect and on a folder change it reads the
 * folder's state; a push from the backend (someone answered, here or in
 * another window) re-decides. A policy refusal is explained once per
 * folder, not on every reconnect, and a backend without the RPC leaves
 * the folder untrusted without an error.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";

import type { Dialog } from "../clientState";
import {
  TRUST_CHANNEL,
  TRUST_DIALOG_KIND,
  isTrustDialogFor,
  isTrustStatus,
  trustFromPush,
  useWorkspaceTrust,
  type WorkspaceTrustStatus,
} from "./model";

afterEach(cleanup);

function status(over: Partial<WorkspaceTrustStatus> = {}): WorkspaceTrustStatus {
  return {
    state: "ask",
    can_trust: true,
    reason: "Not decided yet.",
    path: "/work/app",
    remote: null,
    policy_mode: "ask",
    gated: "project tools",
    ...over,
  };
}

function rig(answer: unknown | Error = status()) {
  const listeners = new Set<(m: unknown) => void>();
  const client = {
    rpc: vi.fn(async () => {
      if (answer instanceof Error) throw answer;
      return answer;
    }),
    onEvent: vi.fn((fn: (m: unknown) => void) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    }),
  };
  const windowModel = { openDialog: vi.fn(), answerDialog: vi.fn() };
  const push = (payload: unknown, channel = TRUST_CHANNEL) => {
    for (const fn of listeners) fn({ type: "push_notification", channel, payload });
  };
  return { client, windowModel, push, listeners };
}

function mount(
  r: ReturnType<typeof rig>,
  props: { connected?: boolean; projectDir?: string; dialog?: Dialog | null } = {},
) {
  return renderHook(
    (p: { connected: boolean; projectDir: string; dialog: Dialog | null }) =>
      useWorkspaceTrust(
        r.client as never,
        p.connected,
        p.projectDir,
        r.windowModel as never,
        p.dialog,
      ),
    {
      initialProps: {
        connected: props.connected ?? true,
        projectDir: props.projectDir ?? "/work/app",
        dialog: props.dialog ?? null,
      },
    },
  );
}

const trustDialog = (path = "/work/app"): Dialog => ({
  id: "d1",
  spec: { kind: TRUST_DIALOG_KIND, path },
});

describe("reading a status", () => {
  it("accepts only a complete status", () => {
    expect(isTrustStatus(status())).toBe(true);
    expect(isTrustStatus(null)).toBe(false);
    expect(isTrustStatus("ask")).toBe(false);
    expect(isTrustStatus({ ...status(), state: "maybe" })).toBe(false);
    expect(isTrustStatus({ ...status(), can_trust: "yes" })).toBe(false);
    expect(isTrustStatus({ ...status(), path: 3 })).toBe(false);
  });

  it("takes a status only from its own channel", () => {
    expect(trustFromPush({ channel: TRUST_CHANNEL, payload: status() })).toEqual(status());
    expect(trustFromPush({ channel: "other", payload: status() })).toBeNull();
    expect(trustFromPush({ channel: TRUST_CHANNEL, payload: { state: "ask" } })).toBeNull();
    expect(trustFromPush(null)).toBeNull();
  });

  it("knows the trust dialog for a folder", () => {
    expect(isTrustDialogFor(trustDialog(), "/work/app")).toBe(true);
    expect(isTrustDialogFor(trustDialog(), "/work/other")).toBe(false);
    expect(isTrustDialogFor({ id: "x", spec: { kind: "update" } }, "/work/app")).toBe(false);
    expect(isTrustDialogFor(null, "/work/app")).toBe(false);
  });
});

describe("the dialog", () => {
  it("asks nothing before the window is connected", () => {
    const r = rig();
    mount(r, { connected: false });
    expect(r.client.rpc).not.toHaveBeenCalled();
    expect(r.client.onEvent).not.toHaveBeenCalled();
  });

  it("opens when the folder's state is ask", async () => {
    const r = rig();
    mount(r);
    await waitFor(() => expect(r.windowModel.openDialog).toHaveBeenCalledOnce());
    expect(r.client.rpc).toHaveBeenCalledWith("get_workspace_trust");
    expect(r.windowModel.openDialog.mock.calls[0][0]).toMatchObject({
      kind: TRUST_DIALOG_KIND,
      path: "/work/app",
      state: "ask",
    });
  });

  it("does not open a second one over a trust dialog already showing", async () => {
    const r = rig();
    mount(r, { dialog: trustDialog() });
    await waitFor(() => expect(r.client.rpc).toHaveBeenCalled());
    r.push(status());
    expect(r.windowModel.openDialog).not.toHaveBeenCalled();
  });

  it("stays shut for a trusted folder", async () => {
    const r = rig(status({ state: "trusted" }));
    mount(r);
    await waitFor(() => expect(r.client.rpc).toHaveBeenCalled());
    await Promise.resolve();
    expect(r.windowModel.openDialog).not.toHaveBeenCalled();
  });

  it("closes the open dialog once someone has answered for its folder", async () => {
    const r = rig(status({ state: "trusted" }));
    mount(r, { dialog: trustDialog() });
    await waitFor(() => expect(r.windowModel.answerDialog).toHaveBeenCalledWith("d1", null));
  });

  it("leaves a dialog for another folder alone", async () => {
    const r = rig(new Error("unknown method"));
    mount(r, { dialog: trustDialog("/work/other") });
    r.push(status({ state: "trusted" }));
    expect(r.windowModel.answerDialog).not.toHaveBeenCalled();
  });

  it("explains a policy refusal once per folder, not on every reconnect", async () => {
    const refused = status({ state: "untrusted", can_trust: false, reason: "Policy forbids it." });
    const r = rig(refused);
    const view = mount(r);
    await waitFor(() => expect(r.windowModel.openDialog).toHaveBeenCalledOnce());
    view.rerender({ connected: false, projectDir: "/work/app", dialog: null });
    view.rerender({ connected: true, projectDir: "/work/app", dialog: null });
    await waitFor(() => expect(r.client.rpc).toHaveBeenCalledTimes(2));
    await Promise.resolve();
    expect(r.windowModel.openDialog).toHaveBeenCalledOnce();
  });

  it("does not open for a folder someone chose not to trust", async () => {
    const r = rig(status({ state: "untrusted", can_trust: true }));
    mount(r);
    await waitFor(() => expect(r.client.rpc).toHaveBeenCalled());
    await Promise.resolve();
    expect(r.windowModel.openDialog).not.toHaveBeenCalled();
  });

  it("reacts to a push, and ignores other channels and malformed answers", async () => {
    const r = rig({ not: "a status" });
    mount(r);
    await waitFor(() => expect(r.client.rpc).toHaveBeenCalled());
    r.push(status(), "something_else");
    expect(r.windowModel.openDialog).not.toHaveBeenCalled();
    r.push(status());
    expect(r.windowModel.openDialog).toHaveBeenCalledOnce();
  });

  it("asks again when the folder changes", async () => {
    const r = rig(status({ state: "trusted" }));
    const view = mount(r);
    await waitFor(() => expect(r.client.rpc).toHaveBeenCalledTimes(1));
    view.rerender({ connected: true, projectDir: "/work/next", dialog: null });
    await waitFor(() => expect(r.client.rpc).toHaveBeenCalledTimes(2));
  });

  it("stops listening when unmounted, and ignores an answer that arrives after", async () => {
    let resolve: (v: unknown) => void = () => {};
    const r = rig();
    r.client.rpc.mockImplementation(() => new Promise((res) => (resolve = res)));
    const view = mount(r);
    expect(r.listeners.size).toBe(1);
    view.unmount();
    expect(r.listeners.size).toBe(0);
    resolve(status());
    await Promise.resolve();
    await Promise.resolve();
    expect(r.windowModel.openDialog).not.toHaveBeenCalled();
  });
});
