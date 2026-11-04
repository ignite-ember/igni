/**
 * Every call an extension makes says which extension made it.
 *
 * The backend enforces a manifest's `permissions.rpc` against the
 * caller, and refuses the whole `ext.*` namespace when there isn't
 * one — an extension reaches its own service and no other, and with
 * no caller there is nothing to check that against.
 *
 * So the risk this covers is a silent omission: a call wired to a
 * bare `client.rpc` still compiles, still type-checks, and fails only
 * at runtime against a real backend. The failure is loud when it
 * happens, but nothing in this repository would reach it, because
 * nothing here runs a backend.
 */
import { describe, expect, it } from "vitest";

import { ExtensionCaller } from "./client";
import type { IgniClient } from "./client";
import { fe } from "./messages";

type RpcArgs = [string, Record<string, unknown>, number | undefined, string | undefined];

/** An `IgniClient` reduced to the one method `ExtensionCaller` uses. */
function stubClient(): { client: IgniClient; calls: RpcArgs[] } {
  const calls: RpcArgs[] = [];
  const client = {
    rpc: (
      method: string,
      args: Record<string, unknown>,
      timeoutMs?: number,
      caller?: string,
    ) => {
      calls.push([method, args, timeoutMs, caller]);
      return Promise.resolve("ok");
    },
  } as unknown as IgniClient;
  return { client, calls };
}

describe("the rpc_request envelope", () => {
  it("omits caller entirely for igni's own UI", () => {
    // Not `caller: undefined`: the field is absent, so an older
    // backend that has never heard of it parses the message
    // unchanged rather than rejecting an unknown key.
    const message = fe.rpcRequest("get_status", {}, "rpc-1");
    expect("caller" in message).toBe(false);
  });

  it("carries the caller when there is one", () => {
    const message = fe.rpcRequest("ext.acme.scanner.list", {}, "rpc-2", "acme.scanner");
    expect(message).toMatchObject({
      type: "rpc_request",
      method: "ext.acme.scanner.list",
      caller: "acme.scanner",
    });
  });
});

describe("ExtensionCaller", () => {
  it("sets the caller on every call without being asked", () => {
    const { client, calls } = stubClient();
    const scanner = new ExtensionCaller(client, "acme.scanner");

    void scanner.rpc("ext.acme.scanner.list");
    void scanner.rpc("ext.acme.scanner.accept", { id: "v1" });

    expect(calls.map((c) => c[3])).toEqual(["acme.scanner", "acme.scanner"]);
  });

  it("passes method and args through untouched", () => {
    const { client, calls } = stubClient();
    void new ExtensionCaller(client, "acme.scanner").rpc("ext.acme.scanner.accept", {
      id: "v1",
      justification: "known and accepted",
    });

    expect(calls[0][0]).toBe("ext.acme.scanner.accept");
    expect(calls[0][1]).toEqual({ id: "v1", justification: "known and accepted" });
  });

  it("exposes the id it was built with", () => {
    // The host needs it to label logs and to build the frame's
    // origin, and reading it back off the caller is one fewer place
    // for the two to disagree.
    const { client } = stubClient();
    expect(new ExtensionCaller(client, "acme.scanner").extensionId).toBe("acme.scanner");
  });

  it("still defaults the timeout when none is given", () => {
    const { client, calls } = stubClient();
    void new ExtensionCaller(client, "acme.scanner").rpc("ext.acme.scanner.list");
    expect(typeof calls[0][2]).toBe("number");
    expect(calls[0][2]).toBeGreaterThan(0);
  });
});
