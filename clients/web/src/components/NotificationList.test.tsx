// @vitest-environment jsdom
/**
 * Reading a notification, whichever extension it is from.
 *
 * The list used to know Artifacts' verbs. Notifications are the
 * floor's now (igni server's `docs/service-identity.md` §10) and every
 * extension has its own kinds, so a row says what the notification
 * says — which extension, its title, its body, who, and when — and the
 * two ways to get that wrong are tested here: a row that loses one of
 * those, and a row that can be opened when there is nowhere to go.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { NotificationList, when, type FloorNotification } from "./NotificationList";

afterEach(cleanup);

function row(over: Partial<FloorNotification> = {}): FloorNotification {
  return {
    id: "n1",
    origin: "server",
    source: "igni.artifacts",
    kind: "comment",
    title: "Runbook",
    body: "Ada: can we drop step 3?",
    actor: "ada@acme.com",
    target: { page: "igni.artifacts", params: { slug: "runbook", comment: "c1" } },
    created_at: new Date().toISOString(),
    read: false,
    ...over,
  };
}

const NAMES: Record<string, string> = { "igni.artifacts": "Artifacts" };
const nameOf = (id: string) => NAMES[id] ?? id;

describe("what a row says", () => {
  it("names its extension, its title, its body, who and when", () => {
    render(<NotificationList rows={[row()]} sourceName={nameOf} />);

    expect(screen.getByText("Artifacts")).toBeTruthy();
    expect(screen.getByText("Runbook")).toBeTruthy();
    expect(screen.getByText("Ada: can we drop step 3?")).toBeTruthy();
    expect(screen.getByText("ada@acme.com")).toBeTruthy();
    expect(screen.getByText(/just now/)).toBeTruthy();
  });

  it("names an extension it has no name for by its id", () => {
    render(<NotificationList rows={[row({ source: "acme.scanner" })]} sourceName={nameOf} />);

    expect(screen.getByText("acme.scanner")).toBeTruthy();
  });

  it("draws a notice about your own work without an actor", () => {
    // An extension's local notice — "your export is ready" — has
    // nobody who did it.
    render(
      <NotificationList
        rows={[row({ origin: "local", actor: "", title: "Runbook.pdf", body: "Your export is ready" })]}
        sourceName={nameOf}
      />,
    );

    expect(screen.getByText("Runbook.pdf")).toBeTruthy();
    expect(screen.queryByText("ada@acme.com")).toBeNull();
  });

  it("puts a server row and a local row with the same id side by side", () => {
    const { container } = render(
      <NotificationList rows={[row(), row({ origin: "local" })]} sourceName={nameOf} />,
    );

    expect(container.querySelectorAll(".notify-row").length).toBe(2);
  });
});

describe("opening one", () => {
  it("hands back the whole notification", () => {
    const opened = vi.fn();
    render(<NotificationList rows={[row()]} onOpen={opened} />);

    fireEvent.click(screen.getByRole("button"));

    expect(opened).toHaveBeenCalledWith(expect.objectContaining({ id: "n1", target: row().target }));
  });

  it("cannot be opened when its page is not here", () => {
    // The extension it is from is not installed on this machine.
    const opened = vi.fn();
    render(<NotificationList rows={[row()]} canOpen={() => false} onOpen={opened} />);

    const button = screen.getByRole("button") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(opened).not.toHaveBeenCalled();
  });

  it("cannot be opened when it names no page", () => {
    const opened = vi.fn();
    render(<NotificationList rows={[row({ target: null })]} onOpen={opened} />);

    expect((screen.getByRole("button") as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("what it looks like when there is nothing", () => {
  it("says so rather than showing an empty box", () => {
    render(<NotificationList rows={[]} />);

    expect(screen.getByText("Nothing is waiting on you.")).toBeTruthy();
  });

  it("marks what has been read without hiding it", () => {
    const { container } = render(<NotificationList rows={[row({ read: true })]} />);

    expect(container.querySelector(".notify-row.is-read")).toBeTruthy();
    expect(screen.getByText("Runbook")).toBeTruthy();
  });
});

describe("when it happened", () => {
  it("reads as just now for something that just did", () => {
    expect(when(new Date().toISOString())).toBe("just now");
  });

  it("counts in minutes, then hours, then days", () => {
    const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
    expect(when(ago(10 * 60_000))).toBe("10m ago");
    expect(when(ago(3 * 3_600_000))).toBe("3h ago");
    expect(when(ago(2 * 86_400_000))).toBe("2d ago");
  });

  it("says nothing rather than NaN for a timestamp it cannot read", () => {
    expect(when("not a date")).toBe("");
    expect(when("")).toBe("");
    expect(when(null)).toBe("");
  });
});
