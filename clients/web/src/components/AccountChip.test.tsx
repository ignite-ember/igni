// @vitest-environment jsdom
/**
 * The header names whoever is signed in, and offers "Log in" only when
 * nobody is.
 *
 * Who is signed in is `cloud_email`, the address on disk — the same
 * thing the sign-in wall goes by. `cloud_connected` says whether the
 * session holds a live token for the igni server, which an air-gapped,
 * on-prem install can lack while the person is plainly signed in. The
 * chip used to switch on `cloud_connected`, so that person was offered
 * "Log in".
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { AccountChip } from "./AccountChip";

afterEach(() => cleanup());

function chip(status: { cloud_email?: string; cloud_connected: boolean } | null) {
  const onOpenAccount = vi.fn();
  const onLogIn = vi.fn();
  render(
    <AccountChip
      status={status}
      name={status?.cloud_email || "Account"}
      onOpenAccount={onOpenAccount}
      onLogIn={onLogIn}
    />,
  );
  return { onOpenAccount, onLogIn };
}

describe("the account chip", () => {
  it("names a signed-in person whose session is not connected", () => {
    const { onOpenAccount } = chip({ cloud_email: "ann@acme.example", cloud_connected: false });
    expect(screen.queryByText("Log in")).toBeNull();
    fireEvent.click(screen.getByText("ann@acme.example"));
    expect(onOpenAccount).toHaveBeenCalledOnce();
  });

  it("says the session is not connected, on the chip's title", () => {
    chip({ cloud_email: "ann@acme.example", cloud_connected: false });
    expect(screen.getByRole("button").getAttribute("title")).toMatch(/not connected/i);
  });

  it("names a signed-in, connected person", () => {
    chip({ cloud_email: "ann@acme.example", cloud_connected: true });
    expect(screen.getByText("ann@acme.example")).toBeTruthy();
    expect(screen.getByRole("button").getAttribute("title")).toBe("Account");
    expect(screen.queryByText("Log in")).toBeNull();
  });

  it("offers Log in when nobody is signed in", () => {
    const { onLogIn } = chip({ cloud_email: "", cloud_connected: false });
    fireEvent.click(screen.getByText("Log in"));
    expect(onLogIn).toHaveBeenCalledOnce();
  });

  it("offers Log in when nobody is signed in, even with a token", () => {
    chip({ cloud_email: "", cloud_connected: true });
    expect(screen.getByText("Log in")).toBeTruthy();
  });

  it("shows neither before the first status arrives", () => {
    const { container } = render(
      <AccountChip status={null} name="Account" onOpenAccount={() => {}} onLogIn={() => {}} />,
    );
    expect(container.textContent).toBe("");
  });
});
