// @vitest-environment jsdom
/**
 * Reading a notification.
 *
 * The queue used to be a list of comments, so every row said the one
 * thing a comment says. Each row is a verb now, and the two ways to
 * get that wrong are both tested here: a verb rendered as the wrong
 * words, and a verb this build has never heard of rendered as
 * nothing at all — which is a notification the reader never learns
 * about, and worse than a plain line saying something happened.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { NotificationList, when, type Notification } from "./NotificationList";

afterEach(cleanup);

function row(over: Partial<Notification> = {}): Notification {
  return {
    id: "n1",
    kind: "comment",
    actor_email: "sam@acme.test",
    slug: "abc123",
    title: "Session handling",
    body: "This contradicts the JWT decision.",
    created_at: new Date().toISOString(),
    read: false,
    ...over,
  };
}

describe("what each verb says", () => {
  it("names the person, the act and the document", () => {
    render(<NotificationList rows={[row()]} />);

    const line = screen.getByText(/commented on/).textContent ?? "";
    expect(line).toContain("sam@acme.test");
    expect(line).toContain("Session handling");
  });

  it("says asked to read for an access request", () => {
    // The row that could not exist at all while this was a list of
    // comments.
    render(<NotificationList rows={[row({ kind: "access_requested" })]} />);

    expect(screen.getByText(/asked to read/)).toBeTruthy();
  });

  it("says mentioned you, which is not the same as commented", () => {
    render(<NotificationList rows={[row({ kind: "mention" })]} />);

    expect(screen.getByText(/mentioned you in/)).toBeTruthy();
  });

  it("still says something for a verb it has never heard of", () => {
    // A deployment one version ahead sends kinds that did not exist
    // when this shipped.
    render(<NotificationList rows={[row({ kind: "someone_did_a_new_thing" })]} />);

    expect(screen.getByText(/did something on/)).toBeTruthy();
  });

  it("has a name for a document it was not given one for", () => {
    render(<NotificationList rows={[row({ title: "" })]} />);

    expect(screen.getByText(/a document/)).toBeTruthy();
  });
});

describe("opening one", () => {
  it("hands back the slug and the notification", () => {
    const opened = vi.fn();
    render(<NotificationList rows={[row()]} onOpen={opened} />);

    fireEvent.click(screen.getByRole("button"));

    expect(opened).toHaveBeenCalledWith("abc123", "n1");
  });

  it("cannot be opened when the document is gone", () => {
    // ``CASCADE`` removes notifications with their document, but a
    // row read a moment before that happened is still on screen.
    const opened = vi.fn();
    render(<NotificationList rows={[row({ slug: "" })]} onOpen={opened} />);

    // ``toBeDisabled`` is jest-dom's; this suite runs on plain
    // vitest matchers.
    const button = screen.getByRole("button") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(opened).not.toHaveBeenCalled();
  });
});

describe("what it looks like when there is nothing", () => {
  it("says so rather than showing an empty box", () => {
    render(<NotificationList rows={[]} />);

    expect(screen.getByText("Nothing is waiting on you.")).toBeTruthy();
  });

  it("marks what has been seen without hiding it", () => {
    // "What did I look at yesterday" is a question, and a queue that
    // empties itself cannot answer it.
    const { container } = render(<NotificationList rows={[row({ read: true })]} />);

    expect(container.querySelector(".notify-row.is-read")).toBeTruthy();
    expect(screen.getByText(/commented on/)).toBeTruthy();
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
  });
});
