// @vitest-environment jsdom
/**
 * Render tests for the interrupted-assistant banner.
 *
 * Banner mounts inside ``AssistantMessage`` (which lives inside
 * ``ChatItemView``). The banner is invisible on normally-completed
 * assistant bubbles; visible on ``kind: "assistant"`` items with
 * the ``interrupted`` flag set. We exercise it through
 * ``ChatItemView`` so the test sees the real render path including
 * the AssistantMessage wrapping.
 *
 * What we pin:
 *
 *   • Banner is hidden when ``interrupted`` is absent (clean
 *     assistant bubble).
 *   • Banner renders with the right label for each reason
 *     ("Run cancelled" / "Run stopped" / "Run interrupted").
 *   • Three buttons render with the right aria-labels.
 *   • ``role="status"`` + ``aria-live="polite"`` for screen-reader
 *     announcements.
 *   • Clicking Retry / Discard / Edit-prompt fires the right
 *     callback with the assistant item's id.
 *   • Keyboard: Enter → Retry, Backspace → Discard, ``e`` → Edit
 *     prompt. The banner is ``tabIndex={0}`` so it can receive
 *     focus; the test simulates that via ``focus()``.
 */

import { describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach } from "vitest";
import { ChatItemView } from "./ChatItems";
import type { ChatItem } from "../chat/model";

afterEach(() => cleanup());

const assistant = (id: number, interrupted?: "cancelled" | "errored" | "abandoned"): ChatItem =>
  interrupted ? { kind: "assistant", id, text: "partial response", interrupted } : { kind: "assistant", id, text: "partial response" };

describe("AssistantMessage banner — hidden on completed runs", () => {
  it("does not render any banner when interrupted is absent", () => {
    render(<ChatItemView item={assistant(1)} />);
    // The banner container is the only thing with
    // ``role="status"`` in the chat tree, so this is a clean
    // sentinel for "did anything render at all?".
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("AssistantMessage banner — visible on interrupted runs", () => {
  it.each(["cancelled", "errored", "abandoned"] as const)(
    "renders the right label for reason=%s",
    (reason) => {
      render(<ChatItemView item={assistant(1, reason)} />);
      const banner = screen.getByRole("status");
      const labels: Record<typeof reason, string> = {
        cancelled: "Run cancelled",
        errored: "Run stopped",
        abandoned: "Run interrupted",
      };
      expect(banner.textContent).toContain(labels[reason]);
    },
  );

  it("renders all three action buttons", () => {
    render(<ChatItemView item={assistant(1, "cancelled")} />);
    // The Retry button is the primary action — find by aria-label
    // so the test doesn't break if the visible label changes.
    expect(screen.getByRole("button", { name: /retry: run cancelled/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /edit prompt for retry/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /discard the partial response/i })).toBeTruthy();
  });

  it("applies the reason-specific CSS modifier class", () => {
    render(<ChatItemView item={assistant(1, "errored")} />);
    const banner = screen.getByRole("status");
    expect(banner.className).toContain("msg-assistant-interrupted--errored");
  });

  it("sets aria-live='polite' so screen readers announce state changes", () => {
    render(<ChatItemView item={assistant(1, "abandoned")} />);
    const banner = screen.getByRole("status");
    expect(banner.getAttribute("aria-live")).toBe("polite");
  });
});

describe("AssistantMessage banner — click handlers", () => {
  it("Retry click fires onRetryInterrupted with the assistant item id", () => {
    const onRetry = vi.fn();
    render(
      <ChatItemView
        item={assistant(42, "cancelled")}
        onRetryInterrupted={onRetry}
      />,
    );
    // Anchor on the primary action's class so the test isn't
    // ambiguous with the Edit-prompt button (which contains
    // "retry" in its aria-label).
    fireEvent.click(document.querySelector(".btn.btn-primary")!);
    expect(onRetry).toHaveBeenCalledWith(42);
  });

  it("Discard click fires onDiscardInterrupted with the assistant item id", () => {
    const onDiscard = vi.fn();
    render(
      <ChatItemView
        item={assistant(7, "errored")}
        onDiscardInterrupted={onDiscard}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /discard the partial response/i }));
    expect(onDiscard).toHaveBeenCalledWith(7);
  });

  it("Edit-prompt click fires onEditPromptFromAssistant with the assistant item id", () => {
    const onEditPrompt = vi.fn();
    render(
      <ChatItemView
        item={assistant(99, "abandoned")}
        onEditPromptFromAssistant={onEditPrompt}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /edit prompt for retry/i }));
    expect(onEditPrompt).toHaveBeenCalledWith(99);
  });

  it("disabled buttons do not fire callbacks", () => {
    // When the parent App.tsx doesn't pass a handler (e.g. a
    // test fixture), the buttons must be disabled rather than
    // throwing on click. Use the unique aria-label for the
    // Retry button — ``Retry: …`` — to disambiguate from the
    // Edit-prompt button which also has "retry" in its label.
    render(<ChatItemView item={assistant(1, "cancelled")} />);
    const retryBtn = screen.getByRole("button", { name: /retry: run cancelled/i }) as HTMLButtonElement;
    expect(retryBtn.disabled).toBe(true);
  });

  it("disabled Discard button does not throw on click", () => {
    render(<ChatItemView item={assistant(1, "cancelled")} />);
    const discardBtn = screen.getByRole("button", { name: /discard the partial response/i }) as HTMLButtonElement;
    expect(discardBtn.disabled).toBe(true);
  });

  it("disabled Edit-prompt button does not throw on click", () => {
    render(<ChatItemView item={assistant(1, "cancelled")} />);
    const editBtn = screen.getByRole("button", { name: /edit prompt for retry/i }) as HTMLButtonElement;
    expect(editBtn.disabled).toBe(true);
  });
});

describe("AssistantMessage banner — keyboard shortcuts", () => {
  it("Enter triggers Retry", () => {
    const onRetry = vi.fn();
    render(
      <ChatItemView
        item={assistant(1, "cancelled")}
        onRetryInterrupted={onRetry}
      />,
    );
    const banner = screen.getByRole("status") as HTMLElement;
    banner.focus();
    fireEvent.keyDown(banner, { key: "Enter" });
    expect(onRetry).toHaveBeenCalledWith(1);
  });

  it("Backspace triggers Discard", () => {
    const onDiscard = vi.fn();
    render(
      <ChatItemView
        item={assistant(1, "cancelled")}
        onDiscardInterrupted={onDiscard}
      />,
    );
    const banner = screen.getByRole("status") as HTMLElement;
    banner.focus();
    fireEvent.keyDown(banner, { key: "Backspace" });
    expect(onDiscard).toHaveBeenCalledWith(1);
  });

  it("Delete also triggers Discard (matches editor muscle memory)", () => {
    const onDiscard = vi.fn();
    render(
      <ChatItemView
        item={assistant(1, "cancelled")}
        onDiscardInterrupted={onDiscard}
      />,
    );
    const banner = screen.getByRole("status") as HTMLElement;
    banner.focus();
    fireEvent.keyDown(banner, { key: "Delete" });
    expect(onDiscard).toHaveBeenCalledWith(1);
  });

  it("'e' triggers Edit prompt", () => {
    const onEditPrompt = vi.fn();
    render(
      <ChatItemView
        item={assistant(1, "cancelled")}
        onEditPromptFromAssistant={onEditPrompt}
      />,
    );
    const banner = screen.getByRole("status") as HTMLElement;
    banner.focus();
    fireEvent.keyDown(banner, { key: "e" });
    expect(onEditPrompt).toHaveBeenCalledWith(1);
  });

  it("unrelated keys do nothing", () => {
    // Belt-and-braces — make sure the keyboard handler isn't
    // greedy. Esc, Tab, arrow keys, letters other than 'e' must
    // not fire any action.
    const onRetry = vi.fn();
    const onDiscard = vi.fn();
    const onEditPrompt = vi.fn();
    render(
      <ChatItemView
        item={assistant(1, "cancelled")}
        onRetryInterrupted={onRetry}
        onDiscardInterrupted={onDiscard}
        onEditPromptFromAssistant={onEditPrompt}
      />,
    );
    const banner = screen.getByRole("status") as HTMLElement;
    banner.focus();
    fireEvent.keyDown(banner, { key: "Escape" });
    fireEvent.keyDown(banner, { key: "Tab" });
    fireEvent.keyDown(banner, { key: "ArrowDown" });
    fireEvent.keyDown(banner, { key: "x" });
    expect(onRetry).not.toHaveBeenCalled();
    expect(onDiscard).not.toHaveBeenCalled();
    expect(onEditPrompt).not.toHaveBeenCalled();
  });
});

describe("AssistantMessage banner — partial content preservation", () => {
  it("the partial assistant text is still visible above the banner", () => {
    // Critical UX guarantee from [[feedback-interrupted-messages]]:
    // the user must see what was streamed so they can decide
    // between retry / discard / edit. The banner never replaces
    // the partial content — it appears below it.
    render(<ChatItemView item={assistant(1, "cancelled")} />);
    // Markdown renders the text via react-markdown; we don't pin
    // the exact wrapping, just confirm the source text is on the
    // page.
    expect(screen.getByText("partial response")).toBeTruthy();
    // And the banner is in the DOM too.
    expect(screen.getByRole("status")).toBeTruthy();
  });
});
