// @vitest-environment jsdom
/**
 * Tests for ``UpdatePrompt`` — the modal that asks the user to
 * install a pending update.
 *
 * Two host modes:
 *   • Tauri (window.__TAURI__.core.invoke present) — button
 *     reads "Install & restart", click invokes
 *     ``ember_install_update`` and the app relaunches itself.
 *   • Plain web — button reads "Download", click opens the
 *     download URL in a new tab.
 *
 * Critical contracts:
 *   • Backdrop click dismisses, but ONLY when not in the
 *     middle of an install (no aborting mid-install or the user
 *     gets a half-replaced binary).
 *   • Errors from the invoke surface inline so the user sees
 *     WHY the install failed.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { UpdatePrompt, type UpdatePromptInfo } from "./UpdatePrompt";

const info: UpdatePromptInfo = {
  available: true,
  current_version: "0.7.5",
  latest_version: "0.8.0",
  download_url: "https://example.com/install",
};

afterEach(() => {
  cleanup();
  // Reset whichever __TAURI__ stub the prior test installed.
  delete (window as { __TAURI__?: unknown }).__TAURI__;
});

describe("UpdatePrompt — version copy", () => {
  it("renders both current + latest versions", () => {
    // The user wouldn't click "install" without seeing both.
    // Pinning the literal strings (not just the wrapping copy)
    // because the numbers are the only signal that matters here.
    render(<UpdatePrompt info={info} onDismiss={() => undefined} />);
    expect(screen.getByText("0.7.5")).toBeTruthy();
    expect(screen.getByText("0.8.0")).toBeTruthy();
  });

  it("renders the 'Later' button + the primary action", () => {
    render(<UpdatePrompt info={info} onDismiss={() => undefined} />);
    expect(screen.getByRole("button", { name: "Later" })).toBeTruthy();
    // Primary action label flips per host; default (no Tauri)
    // is "Download".
    expect(screen.getByRole("button", { name: "Download" })).toBeTruthy();
  });
});

describe("UpdatePrompt — Tauri host", () => {
  beforeEach(() => {
    // Stub the Tauri global to flip the host branch.
    (window as { __TAURI__?: unknown }).__TAURI__ = {
      core: { invoke: vi.fn().mockResolvedValue(undefined) },
    };
  });

  it("primary button reads 'Install & restart' under Tauri", () => {
    render(<UpdatePrompt info={info} onDismiss={() => undefined} />);
    expect(screen.getByRole("button", { name: "Install & restart" })).toBeTruthy();
  });

  it("clicking install fires ember_install_update via invoke", () => {
    const invoke = (window as { __TAURI__?: { core?: { invoke?: ReturnType<typeof vi.fn> } } })
      .__TAURI__!.core!.invoke!;
    render(<UpdatePrompt info={info} onDismiss={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Install & restart" }));
    expect(invoke).toHaveBeenCalledWith("ember_install_update");
  });

  it("flips to 'Installing…' while the invoke is in-flight", async () => {
    // ``busy`` state must be visible — otherwise the user
    // doesn't know whether their click registered and may
    // double-click (firing two installs).
    let resolveInvoke: (() => void) | undefined;
    const invoke = vi
      .fn()
      .mockImplementation(() => new Promise<void>((r) => (resolveInvoke = r)));
    (window as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };

    render(<UpdatePrompt info={info} onDismiss={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Install & restart" }));
    // Synchronously after click, the busy label shows + both
    // buttons disable.
    await waitFor(() => {
      expect(screen.getByText("Installing…")).toBeTruthy();
    });
    expect(
      (screen.getByText("Installing…").closest("button") as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Later" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    // Release the invoke so the state cleanup doesn't leak.
    resolveInvoke?.();
  });

  it("invoke failure surfaces the error message inline", async () => {
    // The user needs to see WHY — "Installation failed" with no
    // detail is useless. The thrown error's stringification
    // lands in the error pill.
    (window as { __TAURI__?: unknown }).__TAURI__ = {
      core: { invoke: vi.fn().mockRejectedValue("disk-full") },
    };
    render(<UpdatePrompt info={info} onDismiss={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Install & restart" }));
    await waitFor(() => {
      expect(screen.getByText(/disk-full/)).toBeTruthy();
    });
    // After failure, busy clears so the user can retry.
    expect(
      (screen.getByRole("button", { name: "Install & restart" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it("backdrop click is BLOCKED while busy (no abort mid-install)", async () => {
    // Closing the modal mid-install would leave the user
    // unable to see the eventual outcome. The source has a
    // ``!busy`` guard on the backdrop handler — pin it.
    let resolveInvoke: (() => void) | undefined;
    (window as { __TAURI__?: unknown }).__TAURI__ = {
      core: {
        invoke: vi
          .fn()
          .mockImplementation(() => new Promise<void>((r) => (resolveInvoke = r))),
      },
    };
    const onDismiss = vi.fn();
    const { container } = render(
      <UpdatePrompt info={info} onDismiss={onDismiss} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Install & restart" }));
    await waitFor(() => {
      expect(screen.getByText("Installing…")).toBeTruthy();
    });
    // Click the backdrop — should be a no-op while busy.
    const backdrop = container.querySelector(".update-prompt-backdrop") as HTMLElement;
    // The handler is wired with a busy guard; the click bubbles
    // through React's delegation but the ``!busy`` check
    // short-circuits.
    fireEvent.click(backdrop);
    expect(onDismiss).not.toHaveBeenCalled();
    resolveInvoke?.();
  });
});

describe("UpdatePrompt — web (no Tauri)", () => {
  it("primary button reads 'Download' under plain web", () => {
    render(<UpdatePrompt info={info} onDismiss={() => undefined} />);
    expect(screen.getByRole("button", { name: "Download" })).toBeTruthy();
  });

  it("clicking Download opens the download URL in a new tab", () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<UpdatePrompt info={info} onDismiss={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(openSpy).toHaveBeenCalledWith("https://example.com/install", "_blank");
    openSpy.mockRestore();
  });

  it("Download click without a download_url silently no-ops", () => {
    // The BE's check_for_update may legitimately return no URL
    // (e.g. user is up to date but the modal lingered from a
    // race). The button must not crash.
    const noUrl: UpdatePromptInfo = { ...info, download_url: undefined };
    const onDismiss = vi.fn();
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<UpdatePrompt info={noUrl} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });
});

describe("UpdatePrompt — dismissal paths", () => {
  it("Later button calls onDismiss", () => {
    const onDismiss = vi.fn();
    render(<UpdatePrompt info={info} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button", { name: "Later" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("backdrop click dismisses when not busy", () => {
    // The standard modal dismissal pattern — clicking outside
    // the dialog closes it (same as Esc, though Esc isn't
    // wired here).
    const onDismiss = vi.fn();
    const { container } = render(
      <UpdatePrompt info={info} onDismiss={onDismiss} />,
    );
    const backdrop = container.querySelector(".update-prompt-backdrop") as HTMLElement;
    // React 19 attaches handlers at the root via delegation —
    // the event must bubble for the handler to fire. Default
    // fireEvent.click bubbles, so just don't pass options.
    fireEvent.click(backdrop);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("click INSIDE the dialog does not dismiss (event-target guard)", () => {
    // The backdrop handler uses ``e.target === e.currentTarget``
    // so clicks bubbling up from the title/body don't close. A
    // refactor to a broader handler would silently break this.
    const onDismiss = vi.fn();
    const { container } = render(
      <UpdatePrompt info={info} onDismiss={onDismiss} />,
    );
    const dialog = container.querySelector(".update-prompt") as HTMLElement;
    fireEvent.click(dialog);
    expect(onDismiss).not.toHaveBeenCalled();
  });
});
