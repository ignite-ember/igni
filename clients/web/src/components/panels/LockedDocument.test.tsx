// @vitest-environment jsdom
/**
 * The page somebody sees when the link they were sent does not open.
 *
 * What it must not be is an error page. Nothing has gone wrong: a
 * colleague sent a link to a document they had not shared, which is
 * an ordinary thing to do, and the person holding it needs to know
 * what it is and ask for it — not to read a status code.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LockedDocument, standing, type LockedState } from "./LockedDocument";

afterEach(cleanup);

const locked = (over: Partial<LockedState> = {}): LockedState => ({
  open: false,
  title: "Pricing model",
  owner_name: "Maya Okonkwo",
  owner_email: "maya@acme.test",
  asked: false,
  declined: false,
  error: "",
  ...over,
});

describe("what it says about where things stand", () => {
  it("names who can let you in", () => {
    expect(standing(locked())).toBe("Maya Okonkwo can give you access.");
  });

  it("says the ask is with them once it has been made", () => {
    expect(standing(locked({ asked: true }))).toContain("has been asked");
  });

  it("says out loud that an earlier ask was declined", () => {
    // Silence after a refusal reads as the request having been lost,
    // and the person raises it again — the one outcome the refusal
    // was meant to prevent.
    expect(standing(locked({ declined: true }))).toContain("declined");
  });

  it("still says something useful when the owner has no name", () => {
    expect(standing(locked({ owner_name: "" }))).toBe("the owner can give you access.");
  });
});

describe("what gets drawn", () => {
  it("names the document", () => {
    // The whole reason the page exists. Without it the ask is "can I
    // have access to, er, one of your documents".
    render(<LockedDocument locked={locked()} busy={false} onAsk={() => {}} />);

    expect(screen.getByText("Pricing model")).toBeTruthy();
  });

  it("does not claim to be an error", () => {
    const { container } = render(
      <LockedDocument locked={locked()} busy={false} onAsk={() => {}} />,
    );

    expect(container.querySelector(".msg-error")).toBeNull();
  });

  it("offers a way to ask", () => {
    render(<LockedDocument locked={locked()} busy={false} onAsk={() => {}} />);

    expect(screen.getByText("Request access")).toBeTruthy();
  });

  it("sends the note along with the ask", () => {
    const asked = vi.fn();
    render(<LockedDocument locked={locked()} busy={false} onAsk={asked} />);

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Reviewing Q3 pricing." },
    });
    fireEvent.click(screen.getByText("Request access"));

    expect(asked).toHaveBeenCalledWith("Reviewing Q3 pricing.");
  });

  it("asks without a note, because a reason is optional", () => {
    // Requiring one turns a two-second ask into a paragraph nobody
    // writes.
    const asked = vi.fn();
    render(<LockedDocument locked={locked()} busy={false} onAsk={asked} />);

    fireEvent.click(screen.getByText("Request access"));

    expect(asked).toHaveBeenCalledWith("");
  });

  it("stops offering once the ask is in", () => {
    // A button that would raise the same request again is a button
    // that fills somebody's queue.
    render(<LockedDocument locked={locked({ asked: true })} busy={false} onAsk={() => {}} />);

    expect(screen.queryByText("Request access")).toBeNull();
    expect(screen.getByText("Waiting on them.")).toBeTruthy();
  });

  it("lets somebody declined ask again", () => {
    // A refusal in March must not silence somebody forever.
    render(<LockedDocument locked={locked({ declined: true })} busy={false} onAsk={() => {}} />);

    expect(screen.getByText("Request access")).toBeTruthy();
  });

  it("refuses a second press while one is in flight", () => {
    const asked = vi.fn();
    render(<LockedDocument locked={locked()} busy onAsk={asked} />);

    fireEvent.click(screen.getByText("…"));

    expect(asked).not.toHaveBeenCalled();
  });

  it("says something even for a document with no title", () => {
    render(<LockedDocument locked={locked({ title: "" })} busy={false} onAsk={() => {}} />);

    expect(screen.getByText("A shared document")).toBeTruthy();
  });
});
