// @vitest-environment jsdom
/**
 * Answering a comment, and deciding whether igni is being asked.
 *
 * One question decides everything else here: is this message a note to
 * colleagues, or an instruction? `@igni` decides it, and only `@igni`
 * — an agent that inferred it from tone would turn every aside in a
 * shared thread into a possible edit.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CommentReplyBox, handleOf, pendingHandle } from "./CommentReplyBox";
import type { IgniClient } from "../../protocol/client";

afterEach(cleanup);

const PEOPLE = ["dana@acme.test", "sam@acme.test"];

function setup(over: { replied?: Record<string, unknown>; people?: string[] } = {}) {
  const rpc = vi.fn(async () => over.replied ?? { ok: true, message: "Replied.", mentioned: [], unknown: [] });
  const onReplied = vi.fn();
  render(
    <CommentReplyBox
      client={{ rpc } as unknown as IgniClient}
      commentId="c1"
      participants={over.people ?? PEOPLE}
      onReplied={onReplied}
    />,
  );
  const box = screen.getByRole("textbox", { name: "Reply to this comment" });
  return { rpc, onReplied, box };
}

const type = (box: HTMLElement, value: string) =>
  fireEvent.change(box, { target: { value, selectionStart: value.length } });

describe("the handle being typed", () => {
  it("is only an unfinished one at the caret", () => {
    // Re-opening the picker because the caret sits after an `@`
    // written ten minutes ago would fight the person writing.
    expect(pendingHandle("@da", 3)).toBe("da");
    expect(pendingHandle("@dana said so", 13)).toBeNull();
  });

  it("ignores an email in the body", () => {
    expect(pendingHandle("mail dana@acme.test", 19)).toBeNull();
  });

  it("opens on a bare @", () => {
    expect(pendingHandle("hello @", 7)).toBe("");
  });
});

describe("handles", () => {
  it("is the local part, because that is what people type", () => {
    expect(handleOf("dana@acme.test")).toBe("dana");
    expect(handleOf("Dana@Acme.Test")).toBe("dana");
  });
});

describe("the picker", () => {
  it("offers the people in the thread and igni", async () => {
    const { box } = setup();
    type(box, "@");
    expect(await screen.findByText("@dana")).toBeTruthy();
    expect(screen.getByText("@sam")).toBeTruthy();
    expect(screen.getByText("@igni")).toBeTruthy();
  });

  it("offers igni even when nobody has spoken", () => {
    // It is the one participant that need not have commented first.
    const { box } = setup({ people: [] });
    type(box, "@");
    expect(screen.getByText("@igni")).toBeTruthy();
  });

  it("narrows as you type", () => {
    const { box } = setup();
    type(box, "@da");
    expect(screen.getByText("@dana")).toBeTruthy();
    expect(screen.queryByText("@sam")).toBeNull();
  });

  it("completes the handle when one is chosen", () => {
    const { box } = setup();
    type(box, "have a look @da");
    fireEvent.mouseDown(screen.getByText("@dana"));
    expect((box as HTMLTextAreaElement).value).toBe("have a look @dana ");
  });

  it("does not let a person take the agent's handle", () => {
    // Reserved: a missed human mention costs a notification, a missed
    // agent mention turns an instruction into a note.
    const { box } = setup({ people: ["igni@acme.test"] });
    type(box, "@ig");
    expect(screen.getAllByText("@igni")).toHaveLength(1);
  });
});

describe("whether igni is being asked", () => {
  it("says so on the button before anything is sent", () => {
    // Before, not after: whether a model runs is the difference
    // between a note and an edit being proposed.
    const { box } = setup();
    type(box, "@igni add the denylist sentence");
    expect(screen.getByRole("button", { name: "Reply and ask igni" })).toBeTruthy();
  });

  it("is an ordinary reply without the mention", () => {
    const { box } = setup();
    type(box, "igni should probably rewrite this");
    expect(screen.getByRole("button", { name: "Reply" })).toBeTruthy();
    expect(screen.getByText(/Goes to the people in this thread/)).toBeTruthy();
  });

  it("warns that a suggestion is not applied on its own", () => {
    const { box } = setup();
    type(box, "@igni fix it");
    expect(screen.getByText(/nothing is applied until you accept it/)).toBeTruthy();
  });
});

describe("sending", () => {
  it("posts the body against the comment", async () => {
    const { rpc, box } = setup();
    type(box, "agreed, I will change it");
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));
    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith("artifact_reply_comment", {
        comment_id: "c1",
        body: "agreed, I will change it",
      }),
    );
  });

  it("clears the box and reloads the queue", async () => {
    const { onReplied, box } = setup();
    type(box, "done");
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));
    await waitFor(() => expect(onReplied).toHaveBeenCalled());
    expect((box as HTMLTextAreaElement).value).toBe("");
  });

  it("names a handle that reached nobody", async () => {
    // Saying only "replied" would leave the author believing they
    // asked somebody a question.
    const { box } = setup({
      replied: { ok: true, message: "Replied.", mentioned: [], unknown: ["dna"] },
    });
    type(box, "@dna thoughts?");
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));
    expect(await screen.findByText(/No one here is called @dna/)).toBeTruthy();
  });

  it("keeps what was written when the send fails", async () => {
    const { box } = setup({ replied: { ok: false, error: "not signed in" } });
    type(box, "worth keeping");
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));
    expect(await screen.findByText("not signed in")).toBeTruthy();
    expect((box as HTMLTextAreaElement).value).toBe("worth keeping");
  });

  it("will not send an empty reply", () => {
    const { rpc } = setup();
    expect(screen.getByRole("button", { name: "Reply" })).toHaveProperty("disabled", true);
    expect(rpc).not.toHaveBeenCalled();
  });

  it("Enter completes an open handle rather than sending", () => {
    // Otherwise a half-typed name is sent to nobody.
    const { rpc, box } = setup();
    type(box, "@da");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(rpc).not.toHaveBeenCalled();
    expect((box as HTMLTextAreaElement).value).toBe("@dana ");
  });
});
