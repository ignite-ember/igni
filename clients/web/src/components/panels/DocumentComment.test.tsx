// @vitest-environment jsdom
/**
 * Component tests for the on-page comment thread.
 *
 * Two things make this more than a form. The exchange stays on the
 * artifact page — sending used to jump to the chat, which took you
 * off the document you were commenting on to watch a reply about it.
 * And there are two ways in, kept apart because the anchor is what
 * makes a comment actionable: one box that sometimes carried a quote
 * and sometimes did not, depending on what happened to be selected,
 * made that difference invisible at the moment it was decided.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createRef, useState } from "react";

import {
  DocumentComment,
  findText,
  personBefore,
  offsetWithin,
  quoteStillPresent,
  threadAtPoint,
  type Thread,
} from "./DocumentComment";
import type { IgniClient } from "../../protocol/client";

// ``cleanup`` unmounts React trees; the document bodies these tests
// append are plain DOM and have to be taken down by hand, or a later
// case makes its selection in one element while the component reads
// another.
const planted: HTMLElement[] = [];

afterEach(() => {
  cleanup();
  planted.splice(0).forEach((el) => el.remove());
  window.getSelection()?.removeAllRanges();
});

type Handler = (m: Record<string, unknown>) => void;

/**
 * A client whose run can be driven event by event, so a test can
 * assert on what is on screen *while* the reply is still arriving.
 */
function fakeClient(posted: unknown = { ok: true, prompt: "PROMPT", message: "" }) {
  // Typed args, so ``mock.calls`` carries the payload rather than an
  // empty tuple a cast has to lie about.
  //
  // The rail reads the document's existing conversation on mount, so
  // this answers that call with nothing. Without it every assertion
  // about "what was posted" would be looking at the load.
  const rpc = vi.fn(async (method: string, _args: Record<string, unknown>) =>
    method === "artifact_comments" ? [] : posted,
  );
  let emit: Handler = () => {};
  let finish: () => void = () => {};
  const runMessage = vi.fn(
    (_text: string, onMessage: Handler) =>
      new Promise<void>((resolve) => {
        emit = onMessage;
        finish = resolve;
      }),
  );
  return {
    client: { rpc, runMessage } as unknown as IgniClient,
    rpc,
    /** What was posted, ignoring the conversation the rail loaded. */
    posts: () =>
      rpc.mock.calls
        .filter(([method]) => method === "artifact_comment")
        .map(([, args]) => args),
    runMessage,
    emit: (m: Record<string, unknown>) => emit(m),
    finish: () => finish(),
  };
}

function mountBody(text = "The service issues a signed JWT on sign-in.") {
  const body = document.createElement("div");
  body.textContent = text;
  document.body.appendChild(body);
  planted.push(body);
  const ref = createRef<HTMLElement>();
  (ref as { current: HTMLElement | null }).current = body;
  return { body, ref };
}

function selectAllOf(node: Node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}

function rightClick(node: Element, x = 40, y = 60) {
  // ``fireEvent`` dispatches inside ``act``, so the state update that
  // opens the menu is flushed before the assertion runs.
  return { defaultPrevented: !fireEvent.contextMenu(node, { clientX: x, clientY: y }) };
}

function setup(over: { posted?: unknown; onRevised?: () => void } = {}) {
  const harness = fakeClient(over.posted);
  const { body, ref } = mountBody();
  render(
    <DocumentComment
      client={harness.client}
      artifactId="a1"
      n={2}
      bodyRef={ref}
      onRevised={over.onRevised}
    />,
  );
  return { ...harness, body, ref };
}

/** Start a fresh thread from the rail's own affordance. */
const newThread = () =>
  fireEvent.click(screen.getByRole("button", { name: "New comment" }));

/** The composer at the foot of the nth thread. */
const composerIn = (index = 0) =>
  document.querySelectorAll(".doc-comment-input")[index] as HTMLTextAreaElement;

const type = (value: string, index = 0) =>
  fireEvent.change(composerIn(index), { target: { value } });

const ask = (index = 0) =>
  fireEvent.click(document.querySelectorAll(".doc-thread-send")[index] as HTMLElement);

/**
 * Wait for a run to settle, then open the thread's reply box again.
 *
 * The box puts itself away on send, so continuing a conversation is
 * Reply → type → send, the same three steps it is on screen.
 */
const reopen = async (index = 0) => {
  await waitFor(() =>
    expect(document.querySelectorAll(".doc-thread-reply")[index]).toBeTruthy(),
  );
  fireEvent.click(document.querySelectorAll(".doc-thread-reply")[index] as HTMLElement);
  await waitFor(() => expect(composerIn(index).disabled).toBe(false));
};

/** Everything the reader sees about comments lives in the rail. */
const rail = () => document.querySelector(".doc-rail") as HTMLElement;

const threads = () => document.querySelectorAll(".doc-conv-item");

function openAnchored(body: HTMLElement) {
  selectAllOf(body);
  rightClick(body);
  fireEvent.click(screen.getByRole("menuitem", { name: "Comment on selection" }));
}

describe("right-clicking a selection", () => {
  it("offers to comment on it", () => {
    const { body } = setup();
    selectAllOf(body);
    rightClick(body);
    expect(screen.getByRole("menuitem", { name: "Comment on selection" })).toBeTruthy();
  });

  it("suppresses the browser menu only when it has something to offer", () => {
    const { body } = setup();
    selectAllOf(body);
    expect(rightClick(body).defaultPrevented).toBe(true);
  });

  it("leaves the browser menu alone with no selection", () => {
    // An app that swallows right-click wholesale takes away copy,
    // search and inspect to offer one item of its own.
    const { body } = setup();
    window.getSelection()?.removeAllRanges();
    expect(rightClick(body).defaultPrevented).toBe(false);
    expect(screen.queryByRole("menuitem")).toBeNull();
  });

  it("leaves the browser menu alone outside the document", () => {
    setup();
    const elsewhere = document.createElement("div");
    elsewhere.textContent = "a version number in the list below";
    document.body.appendChild(elsewhere);
    planted.push(elsewhere);

    selectAllOf(elsewhere);
    expect(rightClick(elsewhere).defaultPrevented).toBe(false);
  });

  it("opens a thread in the rail, not a composer at the pointer", () => {
    const { body } = setup();
    openAnchored(body);

    expect(rail().querySelector(".doc-comment-input")).toBeTruthy();
    expect(document.querySelector(".doc-selection-pop")).toBeNull();
  });

  it("carries the quote onto the thread", () => {
    const { body } = setup();
    openAnchored(body);
    expect(rail().querySelector(".doc-conv-quote")?.textContent).toContain(
      "The service issues a signed JWT",
    );
  });

  it("closes on Escape", () => {
    const { body } = setup();
    selectAllOf(body);
    rightClick(body);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("menuitem")).toBeNull();
  });
});

describe("a thread keeps its own messages", () => {
  const settle = (harness: ReturnType<typeof setup>, text = "Which unit?") => {
    harness.emit({ type: "content_delta", text, is_thinking: false });
    harness.finish();
  };

  it("sends the first message with no thread id, because there is none yet", async () => {
    const { posts } = setup();
    newThread();
    type("Name the TTL.");
    ask();

    await waitFor(() => expect(posts().length).toBe(1));
    expect(posts()[0].thread_id).toBe("");
  });

  it("sends every later message into the same thread", async () => {
    // The whole point. The version with a Reply button made starting
    // a thread the default: eleven comments produced eleven threads,
    // and the history block was never once populated.
    const harness = setup({
      posted: { ok: true, prompt: "P", thread_id: "t-7", message: "" },
    });
    newThread();
    type("First.");
    ask();
    await waitFor(() => expect(harness.rpc).toHaveBeenCalled());
    settle(harness);

    await reopen();
    type("Second.");
    ask();

    await waitFor(() => {
      const second = harness.rpc.mock.calls
        .map((c) => c[1])
        .find((a) => a.body === "Second.");
      expect(second?.thread_id).toBe("t-7");
    });
  });

  it("keeps both messages in one card rather than making two", async () => {
    const harness = setup({
      posted: { ok: true, prompt: "P", thread_id: "t-7", message: "" },
    });
    newThread();
    type("First.");
    ask();
    await screen.findByText("First.");
    settle(harness);
    await reopen();

    type("Second.");
    ask();

    await screen.findByText("Second.");
    expect(threads()).toHaveLength(1);
  });

  it("starts a separate thread from the rail's own affordance", async () => {
    const harness = setup();
    newThread();
    type("First.");
    ask();
    await screen.findByText("First.");
    settle(harness);

    newThread();

    await waitFor(() => expect(threads()).toHaveLength(2));
  });

  it("carries the thread's quote on every message, not just the first", async () => {
    const harness = setup({
      posted: { ok: true, prompt: "P", thread_id: "t-3", message: "" },
    });
    const { body } = harness;
    openAnchored(body);
    type("First.");
    ask();
    await waitFor(() => expect(harness.rpc).toHaveBeenCalled());
    settle(harness);
    await reopen();

    type("Second.");
    ask();

    await waitFor(() => {
      const second = harness.rpc.mock.calls.map((c) => c[1]).find((a) => a.body === "Second.");
      expect(second?.quote).toBe("The service issues a signed JWT on sign-in.");
    });
  });
});

describe("the composer at the foot of a thread", () => {
  it("is there from the start for a comment being started", () => {
    // It exists only because New comment was just pressed. Asking for
    // a second press before the box appears says nothing.
    setup();
    newThread();
    expect(composerIn(0)).toBeTruthy();
  });

  it("rests once the message has been sent", async () => {
    // Including the one that opened the thread. Left open, a thread
    // would look different depending on whether you started it this
    // session or read it back from the store.
    const harness = setup();
    newThread();
    type("First.");
    ask();
    await screen.findByText("First.");
    settleAll(harness);

    await waitFor(() => expect(document.querySelector(".doc-comment-input")).toBeNull());
    expect(document.querySelector(".doc-thread-reply")).toBeTruthy();
  });

  it("invites a reply once something has been said", async () => {
    const harness = setup();
    newThread();
    type("First.");
    ask();
    await screen.findByText("First.");
    settleAll(harness);
    await waitFor(() => expect(document.querySelector(".doc-thread-reply")).toBeTruthy());

    fireEvent.click(document.querySelector(".doc-thread-reply") as HTMLElement);

    expect(composerIn(0).placeholder).toBe("Reply…");
  });

  it("is disabled while the agent is working", async () => {
    // Reopened mid-run it is a box you cannot type a second message
    // into, rather than one that quietly queues one.
    setup();
    newThread();
    type("@igni First.");
    ask();

    await waitFor(() => expect(document.querySelector(".doc-thread-reply")).toBeTruthy());
    fireEvent.click(document.querySelector(".doc-thread-reply") as HTMLElement);

    expect(composerIn(0).disabled).toBe(true);
  });

  it("will not send an empty message", () => {
    const { posts } = setup();
    newThread();
    type("   ");
    expect(
      (document.querySelector(".doc-thread-send") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(posts()).toEqual([]);
  });

  it("clears after sending, so the next message starts empty", async () => {
    const harness = setup();
    newThread();
    type("First.");
    ask();
    await screen.findByText("First.");
    settleAll(harness);
    await waitFor(() => expect(document.querySelector(".doc-thread-reply")).toBeTruthy());

    fireEvent.click(document.querySelector(".doc-thread-reply") as HTMLElement);

    expect(composerIn(0).value).toBe("");
  });
});

function settleAll(harness: ReturnType<typeof setup>) {
  harness.emit({ type: "content_delta", text: "Answered.", is_thinking: false });
  harness.finish();
}

describe("the two directions between a passage and its thread", () => {
  it("clicking a thread leads back to its passage", async () => {
    // The rail is a column of quotes beside a page of prose; without
    // this the reader does the matching.
    const scrolled: unknown[] = [];
    const { body } = setup();
    openAnchored(body);
    // jsdom has no layout, so the call is what is observable.
    for (const el of Array.from(document.querySelectorAll("p, div, span"))) {
      (el as HTMLElement).scrollIntoView = () => scrolled.push(el);
    }

    fireEvent.click(document.querySelector(".doc-conv-anchor") as HTMLElement);

    expect(document.querySelector(".doc-conv-focused")).toBeTruthy();
  });

  it("clicking the selected thread again puts it down", async () => {
    // A selection with no way out but selecting something else
    // leaves the document permanently marked up with an answer the
    // reader has finished with.
    const harness = setup();
    openAnchored(harness.body);
    type("@igni why?");
    ask();
    await screen.findByText(/why\?/);

    fireEvent.click(document.querySelector(".doc-conv-anchor") as HTMLElement);
    expect(document.querySelector(".doc-conv-focused")).toBeNull();
  });

  it("but not while the first message is still being written", async () => {
    // A thread with nothing said in it yet is on screen only because
    // it is selected. Putting it down would take the box somebody is
    // typing into with it.
    const { body } = setup();
    openAnchored(body);
    type("half a thought");

    fireEvent.click(document.querySelector(".doc-conv-anchor") as HTMLElement);

    expect(document.querySelector(".doc-conv-focused")).toBeTruthy();
    expect(composerIn(0).value).toBe("half a thought");
  });

  it("clicking the empty page puts it down as well", async () => {
    // Most of what is on screen is margin: the gutter, the space
    // beside the measure, the room under a short document. A
    // dismissal that only works on the words themselves is one people
    // try twice and conclude does not work.
    const { body } = setup();
    openAnchored(body);
    type("@igni why?");
    ask();
    await screen.findByText(/why\?/);

    // Somewhere that is neither the prose nor the rail.
    const margin = document.createElement("div");
    document.body.appendChild(margin);
    fireEvent.click(margin);

    expect(document.querySelector(".doc-conv-focused")).toBeNull();
  });

  it("but a click inside the rail leaves it alone", async () => {
    // Otherwise reading a long thread would put down the selection
    // that says which passage the thread is about.
    const harness = setup();
    openAnchored(harness.body);
    type("@igni why?");
    ask();
    await screen.findByText(/why\?/);
    fireEvent.click(document.querySelector(".doc-conv-anchor") as HTMLElement);
    fireEvent.click(document.querySelector(".doc-conv-anchor") as HTMLElement);
    expect(document.querySelector(".doc-conv-focused")).toBeTruthy();

    fireEvent.click(document.querySelector(".doc-rail-body") as HTMLElement);

    expect(document.querySelector(".doc-conv-focused")).toBeTruthy();
  });

  it("reading the document puts it down too", async () => {
    // Once the reader is back in the prose the question has been
    // answered, and a ring left over the sentence they are reading is
    // furniture on top of the thing it was pointing at.
    const { body } = setup();
    openAnchored(body);
    expect(document.querySelector(".doc-conv-focused")).toBeTruthy();

    fireEvent.click(body);


    expect(document.querySelector(".doc-conv-focused")).toBeNull();
  });

  it("hit-tests a point against the marked passages", () => {
    // Highlights painted through the Custom Highlight API are not in
    // the DOM and cannot carry a click handler, so the ranges are
    // tested directly.
    const root = document.createElement("div");
    root.innerHTML = "<p>alpha beta gamma</p>";
    document.body.appendChild(root);
    planted.push(root);

    const range = findText(root, "beta")!;
    const found = new Map([["th1", range]]);
    const text = root.querySelector("p")!.firstChild!;

    (document as unknown as Record<string, unknown>).caretRangeFromPoint = () => {
      const r = document.createRange();
      r.setStart(text, 7);
      return r;
    };
    expect(threadAtPoint(found, 1, 1)).toBe("th1");

    (document as unknown as Record<string, unknown>).caretRangeFromPoint = () => {
      const r = document.createRange();
      r.setStart(text, 0);
      return r;
    };
    expect(threadAtPoint(found, 1, 1)).toBeNull();

    delete (document as unknown as Record<string, unknown>).caretRangeFromPoint;
  });

  it("finds nothing where the browser reports no caret", () => {
    expect(threadAtPoint(new Map(), 1, 1)).toBeNull();
  });
});

describe("what each message sends", () => {
  it("carries the passage and where it sat", async () => {
    const { body, posts } = setup();
    openAnchored(body);
    type("Name the TTL.");
    ask();

    await waitFor(() => expect(posts().length).toBe(1));
    const payload = posts()[0];
    expect(payload.quote).toBe("The service issues a signed JWT on sign-in.");
    // An offset, so a document with the same sentence twice can tell
    // the two apart rather than always marking the first.
    expect(payload.quote_start).toBeGreaterThanOrEqual(0);
    expect(payload.quote_end).toBe(
      (payload.quote_start as number) + "The service issues a signed JWT on sign-in.".length,
    );
  });

  it("carries no passage for a comment about the document", async () => {
    const { posts } = setup();
    newThread();
    type("Too long.");
    ask();

    await waitFor(() => expect(posts().length).toBe(1));
    const payload = posts()[0];
    expect(payload.quote).toBe("");
    expect(payload.quote_start).toBe(-1);
    expect(payload.quote_end).toBe(-1);
  });

  it("keeps the quote after the selection is gone", () => {
    // Opening the thread moves focus, and focus moving collapses the
    // selection. The quote is taken at right-click time for this.
    const { body } = setup();
    openAnchored(body);
    window.getSelection()?.removeAllRanges();
    expect(document.querySelector(".doc-conv-quote")?.textContent).toContain(
      "The service issues a signed JWT",
    );
  });

  it("says its scope where a quote would otherwise be", () => {
    setup();
    newThread();
    expect(screen.getByText("About the whole document")).toBeTruthy();
  });
});

describe("the reply, in the rail", () => {
  it("does not run a model for a remark to the people in the thread", async () => {
    // The rule the whole rail now turns on. It used to run one for
    // every message, which was right when the rail was local and the
    // agent was the only other party — and became wrong the moment it
    // started showing threads other people are in. An agent that took
    // every remark as an instruction would set about editing the
    // document each time somebody agreed with somebody else.
    const { runMessage, posts } = setup();
    newThread();
    type("Agreed, and the runbook says the same.");
    ask();

    await waitFor(() => expect(posts().length).toBe(1));
    expect(runMessage).not.toHaveBeenCalled();
  });

  it("leaves no pending answer under a remark", async () => {
    // A spinner under a comment promises a reply nobody is writing.
    const { posts } = setup();
    newThread();
    type("Agreed.");
    ask();

    await waitFor(() => expect(posts().length).toBe(1));
    expect(document.querySelector(".doc-conv-reply")).toBeNull();
  });

  it("says which of the two the button will do, before it is pressed", () => {
    // Whether a model runs is the difference between a remark and an
    // edit being proposed, and it is decided by one word the writer
    // either typed or did not.
    setup();
    newThread();
    type("Agreed.");
    expect(document.querySelector(".doc-thread-send")?.textContent).toBe("Comment");
    type("@igni shorten this.");
    expect(document.querySelector(".doc-thread-send")?.textContent).toBe("Ask igni");
  });

  it("names the convention where the message is written", () => {
    setup();
    newThread();
    type("Agreed.");
    expect(document.querySelector(".doc-thread-hint")?.textContent).toContain("@igni");
  });

  it("runs the turn here rather than handing it to the chat", async () => {
    const { runMessage } = setup();
    newThread();
    type("@igni Why 15 minutes?");
    ask();

    await waitFor(() => expect(runMessage).toHaveBeenCalled());
    expect(runMessage.mock.calls[0][0]).toBe("PROMPT");
  });

  it("streams the answer under the message", async () => {
    const harness = setup();
    newThread();
    type("@igni Why?");
    ask();
    await screen.findByText(/Why\?/);

    harness.emit({ type: "content_delta", text: "Because ", is_thinking: false });
    harness.emit({ type: "content_delta", text: "of load.", is_thinking: false });

    expect(await screen.findByText(/Because of load\./)).toBeTruthy();
  });

  it("keeps the model's thinking out of the reply", async () => {
    const harness = setup();
    newThread();
    type("@igni Why?");
    ask();
    await screen.findByText(/Why\?/);

    harness.emit({ type: "content_delta", text: "hmm, maybe", is_thinking: true });
    harness.emit({ type: "content_delta", text: "Because of load.", is_thinking: false });

    expect(screen.queryByText(/hmm, maybe/)).toBeNull();
    expect(await screen.findByText(/Because of load\./)).toBeTruthy();
  });

  it("says who is working while nothing has come back yet", async () => {
    const harness = setup();
    newThread();
    type("@igni Why?");
    ask();
    await screen.findByText(/Why\?/);

    harness.emit({ type: "run_started", agent_name: "archivist", parent_run_id: "r0" });

    expect(await screen.findByText("archivist is working")).toBeTruthy();
  });

  it("refetches the document once the run ends, because it may be a new version", async () => {
    const onRevised = vi.fn();
    const harness = setup({ onRevised });
    newThread();
    type("@igni Shorten it.");
    ask();
    await screen.findByText(/Shorten it\./);

    harness.finish();

    await waitFor(() => expect(onRevised).toHaveBeenCalled());
  });

  it("reports a failed run in the thread rather than silently stopping", async () => {
    const harness = setup();
    newThread();
    type("@igni Why?");
    ask();
    await screen.findByText(/Why\?/);

    harness.emit({ type: "run_error", error: "the model is unreachable" });

    expect(await screen.findByText("the model is unreachable")).toBeTruthy();
  });

  it("writes the agent's answer back onto the same thread", async () => {
    // So the transcript lives with the document instead of in a chat
    // session that ends when the process does.
    const harness = setup({
      posted: { ok: true, prompt: "P", thread_id: "t-9", message: "" },
    });
    newThread();
    type("@igni Name the TTL.");
    ask();
    await screen.findByText(/Name the TTL\./);

    harness.emit({ type: "content_delta", text: "Done, now v4.", is_thinking: false });
    harness.finish();

    await waitFor(() => {
      const recorded = harness.rpc.mock.calls.map((c) => c[1]).find((a) => a.origin === "agent");
      expect(recorded?.body).toBe("Done, now v4.");
      expect(recorded?.thread_id).toBe("t-9");
    });
  });

  it("records nothing when the run produced no reply", async () => {
    const harness = setup();
    newThread();
    type("Name the TTL.");
    ask();
    await screen.findByText(/Name the TTL\./);
    harness.finish();

    await reopen();
    expect(harness.rpc.mock.calls.map((c) => c[1]).find((a) => a.origin === "agent")).toBeUndefined();
  });
});

describe("when it is refused", () => {
  const refused = { ok: false, message: "Artifacts are disabled.", prompt: "" };

  it("says so on the thread and takes the message back", async () => {
    setup({ posted: refused });
    newThread();
    type("x");
    ask();

    expect(await screen.findByText("Artifacts are disabled.")).toBeTruthy();
    // The turn is removed rather than left hanging with no answer.
    expect(screen.queryByText("x")).toBeNull();
  });

  it("starts no run", async () => {
    const harness = setup({ posted: refused });
    newThread();
    type("x");
    ask();

    await screen.findByText("Artifacts are disabled.");
    expect(harness.runMessage).not.toHaveBeenCalled();
  });
});

describe("a quote whose passage has moved on", () => {
  it("is reported, not left to be noticed", () => {
    // Answering a comment usually rewrites the very passage it was
    // about, so a highlight that stops working is the common case —
    // and is indistinguishable from one that is broken.
    const body = document.createElement("div");
    body.textContent = "It expires after 15 minutes.";
    expect(quoteStillPresent(body, "It expires after 900 seconds.")).toBe(false);
  });

  it("is not reported while the passage is still there", () => {
    const body = document.createElement("div");
    body.textContent = "It expires after 15 minutes.";
    expect(quoteStillPresent(body, "expires after 15 minutes")).toBe(true);
  });

  it("ignores whitespace, which the selection carries and the source does not", () => {
    const body = document.createElement("div");
    body.textContent = "It expires\n  after 15   minutes.";
    expect(quoteStillPresent(body, "It expires after 15 minutes.")).toBe(true);
  });

  it("says nothing about a comment with no quote", () => {
    expect(quoteStillPresent(document.createElement("div"), "")).toBe(true);
  });
});

describe("finding a quoted passage in the document", () => {
  const withMarkup = () => {
    const root = document.createElement("div");
    root.innerHTML = "<p>The token is <strong>opaque</strong> to the client.</p>";
    document.body.appendChild(root);
    planted.push(root);
    return root;
  };

  it("matches across element boundaries", () => {
    // A sentence with a bold word in it is three text nodes. Matching
    // the concatenation is what makes those findable at all.
    const range = findText(withMarkup(), "token is opaque to the client");
    expect(range).not.toBeNull();
    expect(range?.toString()).toContain("opaque");
  });

  it("matches despite collapsed whitespace", () => {
    const root = document.createElement("div");
    root.innerHTML = "<p>one\n   two    three</p>";
    document.body.appendChild(root);
    planted.push(root);
    expect(findText(root, "one two three")).not.toBeNull();
  });

  it("returns nothing when the passage is gone", () => {
    expect(findText(withMarkup(), "a sentence that was rewritten")).toBeNull();
  });

  it("returns nothing for an empty needle rather than matching everything", () => {
    expect(findText(withMarkup(), "")).toBeNull();
  });
});

describe("telling repeated lines apart", () => {
  const twice = () => {
    const root = document.createElement("div");
    root.innerHTML =
      "<p>confirm the key was rotated</p><p>filler</p><p>confirm the key was rotated</p>";
    document.body.appendChild(root);
    planted.push(root);
    return root;
  };

  it("takes the first when nothing says which", () => {
    const root = twice();
    const range = findText(root, "confirm the key was rotated");
    expect(root.firstElementChild?.contains(range!.startContainer)).toBe(true);
  });

  it("takes the one nearest where the comment was made", () => {
    // The bug this fixes: with two identical lines, every comment
    // anchored to the first, and nothing said it was the wrong one.
    const root = twice();
    const late = findText(root, "confirm the key was rotated", 40);
    expect(root.lastElementChild?.contains(late!.startContainer)).toBe(true);
  });

  it("still takes the nearest when the hint has drifted past the end", () => {
    // Offsets go stale the moment anything above them is edited, so
    // the hint has to degrade rather than fail.
    const root = twice();
    const range = findText(root, "confirm the key was rotated", 9999);
    expect(root.lastElementChild?.contains(range!.startContainer)).toBe(true);
  });
});

describe("recording where a passage sat", () => {
  it("counts characters across the elements before it", () => {
    const root = document.createElement("div");
    root.innerHTML = "<p>one</p><p>two</p><p>three</p>";
    document.body.appendChild(root);
    planted.push(root);

    const third = root.lastElementChild!.firstChild!;
    expect(offsetWithin(root, third, 0)).toBe(6);
    expect(offsetWithin(root, third, 2)).toBe(8);
  });

  it("counts characters for a container that is an element, not a text node", () => {
    // Double-click a word or select a whole element and the range's
    // container is the element, with the offset counting children.
    // The version that walked text nodes never found such a
    // container and reported 0 for every one of them — the earliest
    // possible answer, and useless as a tie-break.
    const root = document.createElement("div");
    root.innerHTML = "<p>one</p><p>two</p><p>three</p>";
    document.body.appendChild(root);
    planted.push(root);

    const third = root.lastElementChild!;
    expect(offsetWithin(root, third, 0)).toBe(6);
  });

  it("reports nothing for a node outside the document body", () => {
    const root = document.createElement("div");
    root.innerHTML = "<p>one</p>";
    document.body.appendChild(root);
    planted.push(root);
    const stranger = document.createElement("p");
    expect(offsetWithin(root, stranger, 0)).toBe(-1);
  });
});

// ── Putting the rail away ──────────────────────────────────────────

/**
 * Hidden is held by the page, not by the rail: the width the rail
 * gives up is width the document takes, so the split has to know. The
 * tests drive it the same way, through a controlled wrapper.
 */
function setupHideable(startHidden = false) {
  const harness = fakeClient();
  const { body, ref } = mountBody();
  const onHiddenChange = vi.fn();

  function Host() {
    const [hidden, setHidden] = useState(startHidden);
    return (
      <DocumentComment
        client={harness.client}
        artifactId="a1"
        n={2}
        bodyRef={ref}
        hidden={hidden}
        onHiddenChange={(next) => {
          onHiddenChange(next);
          setHidden(next);
        }}
      />
    );
  }

  render(<Host />);
  return { ...harness, body, ref, onHiddenChange };
}

const hideButton = () => screen.getByRole("button", { name: "Hide comments" });
const stowed = () => document.querySelector(".doc-rail-stowed");

describe("putting the comment rail away", () => {
  it("hides on the control in its own header", () => {
    const { onHiddenChange } = setupHideable();
    expect(stowed()).toBeNull();

    fireEvent.click(hideButton());

    expect(onHiddenChange).toHaveBeenCalledWith(true);
    expect(stowed()).not.toBeNull();
  });

  it("leaves the way back where the rail was", () => {
    // Not in a menu. A reader who has to remember that comments exist
    // before they can look for them has lost them, not hidden them.
    const { onHiddenChange } = setupHideable(true);

    fireEvent.click(screen.getByRole("button", { name: /Show comments/ }));

    expect(onHiddenChange).toHaveBeenCalledWith(false);
    expect(document.querySelector(".doc-rail-head")).not.toBeNull();
  });

  it("is an icon, not the word set sideways", () => {
    // The lane is 30px. "Comments" does not fit across it, and set
    // down it is a column of rotated capitals nobody reads — so the
    // only text here is the count.
    setupHideable(true);
    const button = screen.getByRole("button", { name: "Show comments" });

    expect(button.querySelector("svg")).not.toBeNull();
    expect(button.textContent).toBe("");
  });

  it("keeps saying how many there are", () => {
    // The count is the whole argument for a strip over nothing at all.
    setupHideable();
    newThread();
    type("anything");
    ask();

    fireEvent.click(hideButton());

    expect(stowed()?.textContent).toContain("1");
  });

  it("shows no threads and nothing to write in while away", () => {
    setupHideable();
    newThread();
    fireEvent.click(hideButton());

    expect(threads()).toHaveLength(0);
    expect(document.querySelector(".doc-comment-input")).toBeNull();
    expect(screen.queryByRole("button", { name: "New comment" })).toBeNull();
  });

  it("does not lose a thread that was open", () => {
    // Unmounting would be the cheap way to hide it, and would throw
    // away a running reply along with the text it was about.
    const { rpc } = setupHideable();
    newThread();
    type("the question");
    ask();
    const before = rpc.mock.calls.length;

    fireEvent.click(hideButton());
    fireEvent.click(screen.getByRole("button", { name: /Show comments/ }));

    expect(threads()).toHaveLength(1);
    expect(screen.getByText("the question")).toBeTruthy();
    // Coming back is not a re-send.
    expect(rpc.mock.calls).toHaveLength(before);
  });

  it("asks for the rail back when a comment is started on a passage", () => {
    // Right-click still works with it away; the new thread has to land
    // somewhere the reader can see, or nothing appears to have
    // happened.
    const { body, onHiddenChange } = setupHideable(true);

    openAnchored(body);

    expect(onHiddenChange).toHaveBeenCalledWith(false);
    expect(threads()).toHaveLength(1);
  });
});

describe("narrowing the rail", () => {
  const thread = (over: Partial<Thread> = {}): Thread => ({
    key: "k",
    id: "c",
    rootId: "c",
    quote: "a passage",
    quoteStart: -1,
    turns: [{ key: "t", who: "me", body: "hi", activity: "", phase: "done", error: "" }],
    error: "",
    status: "open",
    n: 3,
    ...over,
  });

  it("keeps everything when nothing is asked for", async () => {
    const { keeps, filterIsEmpty } = await import("./DocumentComment");

    expect(filterIsEmpty({})).toBe(true);
    expect(keeps({}, thread({ status: "rejected" }), false)).toBe(true);
  });

  it("asking for both sides of a question is the same as asking neither", async () => {
    // The reason a facet is a set and not a flag: a flag has no way
    // to say "either", so a reader who ticks both ends up with an
    // empty rail and no idea why.
    const { keeps } = await import("./DocumentComment");
    const both = { status: ["unresolved" as const, "resolved" as const] };

    expect(keeps(both, thread({ status: "open" }), true)).toBe(true);
    expect(keeps(both, thread({ status: "accepted" }), true)).toBe(true);
  });

  it("counts accepted and declined alike as resolved", async () => {
    // Both mean somebody has dealt with it. The difference matters
    // to the person who raised it and not to "what is left".
    const { keeps } = await import("./DocumentComment");
    const settled = { status: ["resolved" as const] };

    expect(keeps(settled, thread({ status: "accepted" }), true)).toBe(true);
    expect(keeps(settled, thread({ status: "rejected" }), true)).toBe(true);
    expect(keeps(settled, thread({ status: "open" }), true)).toBe(false);
  });

  it("combines its clauses rather than replacing them", async () => {
    // The whole point. "What is still open on v3" is one question
    // with two clauses; a row of exclusive tabs makes it a sequence
    // of views to hold in your head.
    const { keeps } = await import("./DocumentComment");
    const both = { status: ["unresolved" as const], versions: [3] };

    expect(keeps(both, thread({ status: "open", n: 3 }), true)).toBe(true);
    expect(keeps(both, thread({ status: "open", n: 4 }), true)).toBe(false);
    expect(keeps(both, thread({ status: "accepted", n: 3 }), true)).toBe(false);
  });

  it("treats a thread with no status as open", async () => {
    // Threads started here have none until they are settled, and
    // hiding what somebody is in the middle of writing is the worst
    // thing a filter can do.
    const { keeps } = await import("./DocumentComment");

    expect(keeps({ status: ["unresolved"] }, thread({ status: undefined }), true)).toBe(
      true,
    );
  });

  it("can hide comments whose passage has been rewritten", async () => {
    const { keeps } = await import("./DocumentComment");

    expect(keeps({ passage: ["here"] }, thread(), false)).toBe(false);
    expect(keeps({ passage: ["here"] }, thread(), true)).toBe(true);
    // And the other way, for finding the ones that need re-anchoring.
    expect(keeps({ passage: ["gone"] }, thread(), false)).toBe(true);
  });

  it("can keep only what igni has answered", async () => {
    const { keeps } = await import("./DocumentComment");
    const answered = thread({
      turns: [
        { key: "a", who: "me", body: "?", activity: "", phase: "done", error: "" },
        { key: "b", who: "agent", body: "!", activity: "", phase: "done", error: "" },
      ],
    });

    expect(keeps({ answer: ["answered"] }, thread(), true)).toBe(false);
    expect(keeps({ answer: ["answered"] }, answered, true)).toBe(true);
    expect(keeps({ answer: ["unanswered"] }, thread(), true)).toBe(true);
  });

  it("takes any of the versions asked for, not all of them", async () => {
    const { keeps } = await import("./DocumentComment");
    const f = { versions: [2, 5] };

    expect(keeps(f, thread({ n: 2 }), true)).toBe(true);
    expect(keeps(f, thread({ n: 5 }), true)).toBe(true);
    expect(keeps(f, thread({ n: 3 }), true)).toBe(false);
  });
});

describe("the chips that say what is being filtered", () => {
  it("names every restriction, one chip each", async () => {
    const { chipsOf } = await import("./DocumentComment");

    const chips = chipsOf({
      status: ["unresolved"],
      answer: ["answered"],
      versions: [4, 2],
    });

    expect(chips.map((c) => c.label)).toEqual([
      "Unresolved",
      "Answered by igni",
      "v4",
      "v2",
    ]);
  });

  it("each takes back only its own", async () => {
    // A chip that cleared the whole facet would take "v4" off with
    // "v2", which is a different filter from the one the reader
    // asked to change.
    const { chipsOf } = await import("./DocumentComment");
    const filter = { status: ["unresolved" as const], versions: [4, 2] };

    const v4 = chipsOf(filter).find((c) => c.label === "v4")!;

    expect(v4.drop(filter)).toEqual({ status: ["unresolved"], versions: [2] });
  });

  it("is empty when nothing is filtered", async () => {
    const { chipsOf } = await import("./DocumentComment");

    expect(chipsOf({})).toEqual([]);
    expect(chipsOf({ versions: [] })).toEqual([]);
  });
});

describe("finding where a quote sits", () => {
  it("does not care how the whitespace fell", async () => {
    // The bug this exists for. A quote is stored as one line; the
    // document wraps it and puts a newline in the middle. Matched
    // literally, the passage is "not found" — so it highlighted fine
    // (that path already squashed) and sorted to the bottom of the
    // rail, which is how the two disagreed.
    const { whereIn } = await import("./DocumentComment");

    expect(whereIn("one two three", "one two")).toBe(0);
    expect(whereIn("one\n  two three", "one two")).toBe(0);
    expect(whereIn("one two three", "one\ntwo")).toBe(0);
  });

  it("picks the occurrence nearest where the comment was left", async () => {
    // A sentence repeated twice is two different passages, and the
    // comment is on one of them.
    const { whereIn } = await import("./DocumentComment");
    const text = "same line. filler filler filler. same line.";

    expect(whereIn(text, "same line.", 32)).toBe(33);
    expect(whereIn(text, "same line.", 0)).toBe(0);
  });

  it("is the first occurrence when nothing says which", async () => {
    const { whereIn } = await import("./DocumentComment");

    expect(whereIn("a. b. a.", "a.", -1)).toBe(0);
  });

  it("says -1 for a passage that is gone", async () => {
    const { whereIn } = await import("./DocumentComment");

    expect(whereIn("what is left", "what was deleted")).toBe(-1);
  });

  it("says -1 for an empty quote rather than matching the start", async () => {
    // Position 0 is a real answer and would be believed — it would
    // sort a comment about the whole document into the first
    // paragraph.
    const { whereIn } = await import("./DocumentComment");

    expect(whereIn("anything", "   ")).toBe(-1);
  });
});

describe("what selects a thread", () => {
  const stored = (over: Record<string, unknown>) => ({
    comment_id: "c1",
    thread_id: "t1",
    parent_id: "",
    origin: "web",
    quote: "The service issues a signed JWT on sign-in.",
    quote_start: -1,
    body: "Which algorithm?",
    status: "open",
    author: "sam@acme.test",
    at: "2026-09-01T10:00:00Z",
    n: 2,
    ...over,
  });

  async function mounted(rows = [stored({})]) {
    const harness = fakeClient();
    harness.rpc.mockImplementation(async (method: string) =>
      method === "artifact_comments" ? rows : { ok: true, prompt: "P", message: "" },
    );
    const { ref } = mountBody();
    render(<DocumentComment client={harness.client} artifactId="a1" n={2} bodyRef={ref} />);
    await screen.findByText("Which algorithm?");
    return document.querySelector(".doc-conv-item") as HTMLElement;
  }

  /** The quote sits on the group, which holds the conversations. */
  const anchor = () => document.querySelector(".doc-conv-anchor") as HTMLElement;

  const isFocused = (card: HTMLElement) => card.classList.contains("doc-conv-focused");

  it("is the comment itself, not only the quote above it", async () => {
    // The sentence in the card *is* the comment. A clickable quote
    // inside an inert card is a small target sitting in a large one.
    const card = await mounted();
    expect(isFocused(card)).toBe(false);

    fireEvent.click(screen.getByText("Which algorithm?"));

    expect(isFocused(card)).toBe(true);
  });

  it("is still the quote", async () => {
    const card = await mounted();

    fireEvent.click(anchor());

    expect(isFocused(card)).toBe(true);
  });

  it("is not the reply box", async () => {
    // Selecting scrolls the document to the passage. Doing that to
    // somebody who has just clicked into the composer moves the page
    // out from under them as they start to type.
    const card = await mounted();
    fireEvent.click(card.querySelector(".doc-thread-reply") as HTMLElement);

    fireEvent.click(card.querySelector("textarea") as HTMLElement);

    expect(isFocused(card)).toBe(false);
  });
});

describe("the two states of a thread's foot", () => {
  const stored = (over: Record<string, unknown> = {}) => ({
    comment_id: "c1",
    thread_id: "t1",
    parent_id: "",
    origin: "web",
    quote: "The service issues a signed JWT on sign-in.",
    quote_start: -1,
    body: "Which algorithm?",
    status: "open",
    author: "sam@acme.test",
    at: "2026-09-01T10:00:00Z",
    n: 2,
    ...over,
  });

  async function withThread() {
    const harness = fakeClient();
    harness.rpc.mockImplementation(async (method: string) =>
      method === "artifact_comments" ? [stored()] : { ok: true, prompt: "P", message: "" },
    );
    const { ref } = mountBody();
    render(<DocumentComment client={harness.client} artifactId="a1" n={2} bodyRef={ref} />);
    await screen.findByText("Which algorithm?");
    return harness;
  }

  const replyLink = () => document.querySelector(".doc-thread-reply") as HTMLElement;
  const box = () => document.querySelector(".doc-comment-input");

  it("rests as the word Reply, with no box", async () => {
    // Twenty conversations used to carry twenty empty textareas, each
    // one looking like a reply somebody started and abandoned.
    await withThread();

    expect(replyLink()).toBeTruthy();
    expect(box()).toBeNull();
  });

  it("opens the box when Reply is pressed", async () => {
    await withThread();

    fireEvent.click(replyLink());

    expect(box()).toBeTruthy();
  });

  it("puts the box away again on Cancel", async () => {
    await withThread();
    fireEvent.click(replyLink());

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(box()).toBeNull();
    expect(replyLink()).toBeTruthy();
  });

  it("keeps the conversation when a reply to it is cancelled", async () => {
    // Backing out of the reply is not backing out of the thread.
    await withThread();
    fireEvent.click(replyLink());

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByText("Which algorithm?")).toBeTruthy();
  });

  it("opens straight into the box for a comment being started", async () => {
    // It exists only because New comment was just pressed. Asking for
    // a second press before the box appears says nothing.
    await withThread();

    newThread();

    expect(document.querySelectorAll(".doc-comment-input")).toHaveLength(1);
  });

  it("discards a new comment nobody wrote in", async () => {
    // Left behind it is an empty card quoting a passage nobody said
    // anything about — indistinguishable from one that failed to load.
    await withThread();
    newThread();
    expect(document.querySelectorAll(".doc-conv-item")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(document.querySelectorAll(".doc-conv-item")).toHaveLength(1);
  });
});

describe("the order the threads are listed in", () => {
  /** Mount the rail over a known document, with a known conversation. */
  function withStored(rows: Record<string, unknown>[], text: string) {
    const harness = fakeClient();
    harness.rpc.mockImplementation(async (method: string) =>
      method === "artifact_comments" ? rows : { ok: true, prompt: "P", message: "" },
    );
    const { ref } = mountBody(text);
    render(<DocumentComment client={harness.client} artifactId="a1" n={2} bodyRef={ref} />);
    return harness;
  }

  const stored = (over: Record<string, unknown>) => ({
    comment_id: "c",
    thread_id: "t",
    parent_id: "",
    origin: "web",
    quote: "",
    quote_start: -1,
    body: "",
    status: "open",
    author: "sam@acme.test",
    at: "2026-09-01T10:00:00Z",
    n: 2,
    ...over,
  });

  // One entry per passage: conversations about the same sentence are
  // grouped under a single quote.
  const quotesShown = () =>
    [...document.querySelectorAll(".doc-conv-group")].map(
      (c) => c.querySelector(".doc-conv-quote")?.textContent ?? "(document)",
    );

  it("follows the document, not the clock", async () => {
    // A margin conversation is read alongside the text. Sorted by
    // recency, the card beside a sentence is nowhere near it, and a
    // comment made this morning on the first paragraph sits below one
    // made last month on the last.
    // The two orders have to disagree, or the test cannot tell which
    // one produced the answer: the newest comment is on the *last*
    // line, so by the clock it would come first.
    withStored(
      [
        stored({ comment_id: "late", thread_id: "t2", quote: "last line", at: "2026-09-09T00:00:00Z" }),
        stored({ comment_id: "early", thread_id: "t1", quote: "first line", at: "2026-09-01T00:00:00Z" }),
      ],
      "first line, then the middle, then the last line.",
    );

    await waitFor(() => expect(quotesShown()).toEqual(["first line", "last line"]));
  });

  it("places a quote whose whitespace has moved, rather than dumping it last", async () => {
    // The reported bug. The document wraps a sentence and puts a
    // newline inside it; the stored quote is one line. Searched
    // literally the passage is "not found", so it highlighted
    // perfectly and sorted to the bottom of the rail — the rail and
    // the document disagreeing about the same sentence.
    withStored(
      [
        stored({ comment_id: "wrapped", thread_id: "t1", quote: "alpha beta gamma." }),
        stored({ comment_id: "later", thread_id: "t2", quote: "then the tail." }),
      ],
      "alpha beta\ngamma. then the tail.",
    );

    await waitFor(() =>
      expect(quotesShown()).toEqual(["alpha beta gamma.", "then the tail."]),
    );
  });

  it("quotes a passage once, however many conversations are on it", async () => {
    // Two remarks on the same sentence, usually a month and a few
    // versions apart, were two cards quoting it back. The repetition
    // reads as a rendering fault before it reads as two
    // conversations — and it states the thing they share twice while
    // burying the thing that differs.
    withStored(
      [
        stored({ comment_id: "a", thread_id: "t1", quote: "first line", body: "early", n: 2 }),
        stored({ comment_id: "b", thread_id: "t2", quote: "first line", body: "late", n: 4 }),
      ],
      "first line, then the last line.",
    );

    await waitFor(() => expect(screen.getByText("late")).toBeTruthy());
    expect(document.querySelectorAll(".doc-conv-quote")).toHaveLength(1);
    // Still two conversations, each keeping its own version.
    expect(document.querySelectorAll(".doc-conv-group")).toHaveLength(1);
    expect(document.querySelectorAll(".doc-conv-group .doc-conv-item")).toHaveLength(2);
    // Oldest first inside a group: the conversations about one
    // sentence are its history, and a history reads forwards.
    expect([...document.querySelectorAll(".doc-conv-at")].map((e) => e.textContent)).toEqual([
      "v2",
      "v4",
    ]);
  });

  it("does not pile together the same words from two different places", async () => {
    // A heading repeated in a document is two passages. Grouping on
    // the text alone would collect the comments from both into one.
    withStored(
      [
        stored({ comment_id: "a", thread_id: "t1", quote: "Limits", body: "up here", quote_start: 0 }),
        stored({
          comment_id: "b",
          thread_id: "t2",
          quote: "Limits",
          body: "down there",
          quote_start: 49,
        }),
      ],
      "Limits, and a good deal of text between, and then Limits again.",
    );

    await waitFor(() => expect(screen.getByText("up here")).toBeTruthy());
    expect(document.querySelectorAll(".doc-conv-group")).toHaveLength(2);
  });

  it("puts a comment about the whole document under the passages", async () => {
    // Where a note on an article goes: at its foot. A remark about
    // the whole thing is made once the whole thing has been read, and
    // above the first passage it sat between the reader and every
    // comment that was actually about a sentence.
    withStored(
      [
        stored({ comment_id: "a", thread_id: "t1", quote: "first line" }),
        stored({ comment_id: "b", thread_id: "t2", quote: "" }),
      ],
      "first line, then the last line.",
    );

    await waitFor(() => expect(quotesShown()).toHaveLength(2));
    expect(quotesShown()).toEqual(["first line", "(document)"]);
  });

  it("still keeps a rewritten passage above a note on the whole document", async () => {
    // Both are filed past the end, so they need an order between
    // them. A comment that was about a sentence belongs with the
    // sentences, however lost that sentence now is.
    withStored(
      [
        stored({ comment_id: "a", thread_id: "t1", quote: "" }),
        stored({ comment_id: "b", thread_id: "t2", quote: "a line since rewritten" }),
        stored({ comment_id: "c", thread_id: "t3", quote: "first line" }),
      ],
      "first line, then the last line.",
    );

    await waitFor(() => expect(quotesShown()).toHaveLength(3));
    expect(quotesShown()).toEqual([
      "first line",
      "a line since rewritten",
      "(document)",
    ]);
  });

  it("puts a rewritten passage last, because it cannot be placed", async () => {
    // Guessing a position for it would sit it beside a sentence it is
    // not about.
    withStored(
      [
        stored({ comment_id: "gone", thread_id: "t1", quote: "a sentence since deleted" }),
        stored({ comment_id: "here", thread_id: "t2", quote: "last line" }),
      ],
      "first line, then the last line.",
    );

    await waitFor(() =>
      expect(quotesShown()).toEqual(["last line", "a sentence since deleted"]),
    );
  });
});

describe("the marks in the document, while the rail is away", () => {
  /** jsdom has neither ``CSS.highlights`` nor ``Highlight``. */
  function stubHighlights() {
    const highlights = new Map<string, unknown>();
    const cssBefore = (globalThis as Record<string, unknown>).CSS;
    vi.stubGlobal("CSS", { highlights });
    vi.stubGlobal(
      "Highlight",
      class {
        constructor(...ranges: Range[]) {
          Object.assign(this, { ranges });
        }
      },
    );
    return { highlights, restore: () => vi.stubGlobal("CSS", cssBefore) };
  }

  afterEach(() => vi.unstubAllGlobals());

  it("takes them down with it", () => {
    // A marked passage is a link to a thread. Leaving the links
    // painted while the thing they lead to is put away gives the
    // reader underlined sentences that do nothing.
    const { highlights } = stubHighlights();
    const { body } = setupHideable();
    openAnchored(body);
    expect(highlights.size).toBeGreaterThan(0);

    fireEvent.click(hideButton());

    expect(highlights.size).toBe(0);
  });

  it("marks the passages behind comments the document already had", async () => {
    // The bug this covers: the marks were computed from the threads
    // started in *this session* only, so a document's existing
    // comments highlighted nothing. Every card in the rail looked
    // alive and half of them were inert.
    const { highlights } = stubHighlights();
    const harness = fakeClient();
    harness.rpc.mockImplementation(async (method: string) =>
      method === "artifact_comments"
        ? [
            {
              comment_id: "c1",
              thread_id: "t1",
              parent_id: "",
              origin: "web",
              quote: "The service issues a signed JWT on sign-in.",
              body: "Which algorithm?",
              status: "open",
              author: "dana@acme.test",
              at: "2026-09-01T10:00:00Z",
              n: 2,
            },
          ]
        : { ok: true, prompt: "PROMPT", message: "" },
    );
    const { ref } = mountBody();
    render(
      <DocumentComment client={harness.client} artifactId="a1" n={2} bodyRef={ref} />,
    );

    await waitFor(() => expect(highlights.size).toBeGreaterThan(0));
  });

  it("paints them again on the way back", () => {
    const { highlights } = stubHighlights();
    const { body } = setupHideable();
    openAnchored(body);
    fireEvent.click(hideButton());

    fireEvent.click(screen.getByRole("button", { name: /Show comments/ }));

    expect(highlights.size).toBeGreaterThan(0);
  });
});

describe("where the way to start a comment lives", () => {
  // It used to be the last child of the scrolling list, which reads
  // fine on a document nobody has commented on and badly on a real
  // one: on a page with a hundred comments the control was a hundred
  // cards down, so starting a new thread meant scrolling to the end
  // of everyone else's.
  it("is not inside the part that scrolls", () => {
    setup();

    const start = screen.getByRole("button", { name: "New comment" });

    expect(start.closest(".doc-rail-body")).toBeNull();
  });

  it("is still inside the rail", () => {
    // Outside the scroller, not outside the column — a footer that
    // escaped the aside would be loose on the page.
    setup();

    const start = screen.getByRole("button", { name: "New comment" });

    expect(start.closest(".doc-rail")).not.toBeNull();
  });
});

describe("replying to a comment the document already had", () => {
  // The rail draws two lists: what this session started, and what was
  // read back from the store. ``send`` looked in the first one only,
  // so a reply to an existing comment found no thread and returned —
  // no post, no turn, no error. It simply vanished.
  const stored = (over: Record<string, unknown> = {}) => ({
    comment_id: "c1",
    thread_id: "local-thread-abc",
    parent_id: "",
    origin: "web",
    quote: "The service issues a signed JWT on sign-in.",
    quote_start: -1,
    body: "Which algorithm?",
    status: "open",
    author: "sam@acme.test",
    at: "2026-09-01T10:00:00Z",
    n: 2,
    ...over,
  });

  async function replyTo(text: string) {
    const harness = fakeClient({ ok: true, prompt: "P", thread_id: "local-thread-abc", message: "" });
    harness.rpc.mockImplementation(async (method: string) =>
      method === "artifact_comments"
        ? [stored()]
        : { ok: true, prompt: "P", thread_id: "local-thread-abc", message: "" },
    );
    const { ref } = mountBody();
    render(<DocumentComment client={harness.client} artifactId="a1" n={2} bodyRef={ref} />);
    await screen.findByText("Which algorithm?");

    fireEvent.click(document.querySelector(".doc-thread-reply") as HTMLElement);
    type(text);
    ask();
    return harness;
  }

  it("posts it", async () => {
    const harness = await replyTo("Because of the lock.");

    await waitFor(() => expect(harness.posts()).toHaveLength(1));
    expect(harness.posts()[0].body).toBe("Because of the lock.");
  });

  it("posts it into the thread it is answering", async () => {
    // Not a new one. The thread id is what keeps a conversation a
    // conversation rather than a column of unrelated remarks.
    const harness = await replyTo("Because of the lock.");

    await waitFor(() => expect(harness.posts()).toHaveLength(1));
    expect(harness.posts()[0].thread_id).toBe("local-thread-abc");
  });

  it("shows it under the comment it answers", async () => {
    await replyTo("Because of the lock.");

    await screen.findByText("Because of the lock.");
    expect(document.querySelectorAll(".doc-conv-item")).toHaveLength(1);
  });

  it("carries the quote of the thread it joins", async () => {
    const harness = await replyTo("Because of the lock.");

    await waitFor(() => expect(harness.posts()).toHaveLength(1));
    expect(harness.posts()[0].quote).toBe("The service issues a signed JWT on sign-in.");
  });
});

describe("telling igni's answer apart from a person's", () => {
  // The agent has no account and never fills in an author, so a
  // byline keyed on ``author`` printed its answer as an
  // unattributed block of prose. In a thread where every other turn
  // carries a name, the one paragraph with no name reads as part of
  // whatever was said above it.
  const stored = (over: Record<string, unknown> = {}) => ({
    comment_id: "c1",
    thread_id: "t1",
    parent_id: "",
    origin: "web",
    quote: "The service issues a signed JWT on sign-in.",
    quote_start: -1,
    body: "What does it mean?",
    status: "open",
    author: "maya@acme.test",
    at: "2026-09-01T10:00:00Z",
    n: 2,
    ...over,
  });

  async function withAnswer(over: Record<string, unknown> = {}) {
    const harness = fakeClient();
    harness.rpc.mockImplementation(async (method: string) =>
      method === "artifact_comments"
        ? [
            stored(),
            stored({
              comment_id: "c2",
              parent_id: "c1",
              origin: "agent",
              body: "It checks on refresh only.",
              at: "2026-09-01T10:05:00Z",
              ...over,
            }),
          ]
        : { ok: true, prompt: "P", message: "" },
    );
    const { ref } = mountBody();
    render(<DocumentComment client={harness.client} artifactId="a1" n={2} bodyRef={ref} />);
    await screen.findByText("It checks on refresh only.");
  }

  it("names the agent on its own turn", async () => {
    await withAnswer();

    expect(document.querySelector(".doc-conv-by-agent")?.textContent).toBe("igni");
  });

  it("names it even though the agent has no author", async () => {
    // The actual bug. An agent row with no author field still has to
    // be attributed — it is the one turn whose author can never
    // arrive from the store.
    await withAnswer({ author: "" });

    expect(document.querySelector(".doc-conv-by-agent")?.textContent).toBe("igni");
  });

  it("draws the line joining it to the turn it answers", async () => {
    // An answer sitting under a question looks exactly like a second
    // question; the order is the only thing saying otherwise, and
    // order is what a reader loses first in a long thread.
    await withAnswer();

    expect(document.querySelectorAll(".doc-conv-hook")).toHaveLength(1);
  });

  it("does not draw one on a person's turn", async () => {
    await withAnswer();

    const mine = document.querySelector(".doc-conv-body");
    expect(mine?.querySelector(".doc-conv-hook")).toBeFalsy();
    expect(mine?.textContent).toContain("maya");
  });
});

describe("which turn an answer is drawn back to", () => {
  // Two agent answers to one question sit one after the other.
  // Anchoring the line to whatever element is directly above makes
  // the second reach back to the first one's last line: a stub
  // joining igni to igni, beside a question neither appears to be
  // about.
  const thread = (...kinds: string[]) => {
    const host = document.createElement("div");
    for (const kind of kinds) {
      const turn = document.createElement("div");
      turn.className = kind === "me" ? "doc-conv-body" : "doc-conv-reply";
      turn.textContent = kind;
      host.appendChild(turn);
    }
    return host;
  };

  it("is the turn above, when a person asked it", () => {
    const host = thread("me", "agent");

    const answered = personBefore(host.children[1]);

    expect(answered).toBe(host.children[0]);
  });

  it("walks back over the agent's own earlier answer", () => {
    const host = thread("me", "agent", "agent");

    const answered = personBefore(host.children[2]);

    expect(answered).toBe(host.children[0]);
  });

  it("walks back over several of them", () => {
    const host = thread("me", "agent", "agent", "agent");

    expect(personBefore(host.children[3])).toBe(host.children[0]);
  });

  it("finds the nearest question when a thread has two", () => {
    // Not simply the first. A second question later in the thread is
    // what the answers after it are about.
    const host = thread("me", "agent", "me", "agent");

    expect(personBefore(host.children[3])).toBe(host.children[2]);
  });

  it("answers nothing when an agent turn opens the thread", () => {
    // Possible for a thread projected back out of the store without
    // the remark that prompted it. Nothing to draw to.
    const host = thread("agent");

    expect(personBefore(host.children[0])).toBeNull();
  });
});

describe("when each turn was written", () => {
  // A thread is an argument in order, and without dates the order is
  // the only thing carrying it — which breaks the moment a passage is
  // commented on twice a month apart and the two remarks sit next to
  // each other looking like one exchange.
  const stored = (over: Record<string, unknown> = {}) => ({
    comment_id: "c1",
    thread_id: "t1",
    parent_id: "",
    origin: "web",
    quote: "The service issues a signed JWT on sign-in.",
    quote_start: -1,
    body: "What does it mean?",
    status: "open",
    author: "maya@acme.test",
    at: "2026-09-27T16:11:09.000Z",
    n: 2,
    ...over,
  });

  async function withTurns(rows: Record<string, unknown>[]) {
    const harness = fakeClient();
    harness.rpc.mockImplementation(async (method: string) =>
      method === "artifact_comments" ? rows : { ok: true, prompt: "P", message: "" },
    );
    const { ref } = mountBody();
    render(<DocumentComment client={harness.client} artifactId="a1" n={2} bodyRef={ref} />);
    await screen.findByText("What does it mean?");
    return harness;
  }

  const stamps = () =>
    [...document.querySelectorAll(".doc-conv-when")] as HTMLTimeElement[];

  it("dates a comment read back from the store", async () => {
    await withTurns([stored()]);

    expect(stamps()).toHaveLength(1);
    expect(stamps()[0].dateTime).toBe("2026-09-27T16:11:09.000Z");
  });

  it("dates the agent's answer too", async () => {
    // The agent's turn is the one with no author, and an earlier
    // version of this hung the whole byline row on the author field.
    await withTurns([
      stored(),
      stored({
        comment_id: "c2",
        parent_id: "c1",
        origin: "agent",
        author: "",
        body: "It checks on refresh only.",
        at: "2026-09-27T16:12:00.000Z",
      }),
    ]);

    expect(stamps()).toHaveLength(2);
    expect(stamps()[1].dateTime).toBe("2026-09-27T16:12:00.000Z");
  });

  it("carries the exact moment for hovering", async () => {
    // "3d ago" is the right length for a rail and the wrong amount
    // of detail for working out what happened in what order.
    await withTurns([stored()]);

    expect(stamps()[0].title).toMatch(/2026/);
  });

  it("dates a comment the moment it is written", async () => {
    // Not left blank until a reload. A comment just sent would
    // otherwise be the one turn in the thread with no date on it.
    setup();
    newThread();
    type("Fresh.");
    ask();

    await screen.findByText("Fresh.");
    expect(stamps().length).toBeGreaterThan(0);
    expect(stamps()[0].dateTime).toBeTruthy();
  });
});

describe("what the document is marked for, once the rail is narrowed", () => {
  /**
   * The marks are the other half of the filter's answer.
   *
   * Narrowing the rail to unresolved comments and leaving every
   * passage lit says the filter did nothing. A reader who narrows is
   * asking which passages still matter, not which ones ever did —
   * and this went unnoticed with a green suite because nothing
   * looked at the marks and the filter together.
   */
  function stubHighlights() {
    const highlights = new Map<string, unknown>();
    vi.stubGlobal("CSS", { highlights });
    vi.stubGlobal(
      "Highlight",
      class {
        constructor(...ranges: Range[]) {
          Object.assign(this, { ranges, size: ranges.length });
        }
      },
    );
    return highlights;
  }

  afterEach(() => vi.unstubAllGlobals());

  const stored = (over: Record<string, unknown>) => ({
    comment_id: "c",
    thread_id: "t",
    parent_id: "",
    origin: "web",
    quote: "",
    quote_start: -1,
    body: "…",
    status: "open",
    author: "maya@acme.test",
    at: "2026-09-01T10:00:00Z",
    n: 2,
    ...over,
  });

  /** Two passages, one settled and one not. */
  async function withTwo() {
    const harness = fakeClient();
    harness.rpc.mockImplementation(async (method: string) =>
      method === "artifact_comments"
        ? [
            stored({
              comment_id: "open1",
              thread_id: "t1",
              quote: "The service issues a signed JWT on sign-in.",
              body: "Still open",
              status: "open",
            }),
            stored({
              comment_id: "done1",
              thread_id: "t2",
              quote: "The token is read on every request",
              body: "Settled",
              status: "accepted",
            }),
          ]
        : { ok: true, prompt: "P", message: "" },
    );
    const { ref } = mountBody(
      "The service issues a signed JWT on sign-in. The token is read on every request and is opaque.",
    );
    render(<DocumentComment client={harness.client} artifactId="a1" n={2} bodyRef={ref} />);
    await screen.findByText("Still open");
  }

  /** How many passages the document is currently marked for. */
  const marked = (highlights: Map<string, unknown>) => {
    const mark = highlights.get("doc-comment-mark") as { ranges?: unknown[] } | undefined;
    return mark?.ranges?.length ?? 0;
  };

  it("marks both passages before anything is filtered", async () => {
    const highlights = stubHighlights();
    await withTwo();

    expect(marked(highlights)).toBe(2);
  });

  it("stops marking the passage whose comment was filtered away", async () => {
    const highlights = stubHighlights();
    await withTwo();

    // Narrow to unresolved. The settled one leaves the rail, and
    // its passage has to stop being lit with it.
    fireEvent.click(screen.getByRole("button", { name: /Filter/ }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /Unresolved/ }));

    await waitFor(() => expect(marked(highlights)).toBe(1));
  });

  it("marks them both again when the filter is cleared", async () => {
    const highlights = stubHighlights();
    await withTwo();
    fireEvent.click(screen.getByRole("button", { name: /Filter/ }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /Unresolved/ }));
    await waitFor(() => expect(marked(highlights)).toBe(1));

    fireEvent.click(screen.getByRole("checkbox", { name: /Unresolved/ }));

    await waitFor(() => expect(marked(highlights)).toBe(2));
  });
});
