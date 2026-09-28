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
  offsetWithin,
  quoteStillPresent,
  threadAtPoint,
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

    await waitFor(() => expect(composerIn(0).disabled).toBe(false));
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
    await waitFor(() => expect(composerIn(0).disabled).toBe(false));

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
    await waitFor(() => expect(composerIn(0).disabled).toBe(false));

    type("Second.");
    ask();

    await waitFor(() => {
      const second = harness.rpc.mock.calls.map((c) => c[1]).find((a) => a.body === "Second.");
      expect(second?.quote).toBe("The service issues a signed JWT on sign-in.");
    });
  });
});

describe("the composer at the foot of a thread", () => {
  it("is there from the start, not behind a button", () => {
    setup();
    newThread();
    expect(composerIn(0)).toBeTruthy();
  });

  it("invites a reply once something has been said", async () => {
    const harness = setup();
    newThread();
    type("First.");
    ask();
    await screen.findByText("First.");
    harness.emit({ type: "content_delta", text: "Answered.", is_thinking: false });
    harness.finish();

    await waitFor(() => expect(composerIn(0).placeholder).toBe("Reply…"));
  });

  it("is disabled while the agent is working", async () => {
    setup();
    newThread();
    type("@igni First.");
    ask();
    await waitFor(() => expect(composerIn(0).disabled).toBe(true));
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
    await waitFor(() => expect(composerIn(0).value).toBe(""));
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

    await waitFor(() => expect(composerIn(0).disabled).toBe(false));
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
