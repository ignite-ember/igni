// @vitest-environment jsdom
/**
 * Reading a document's existing conversation back into the rail.
 *
 * The rail used to start empty every time it was opened, holding only
 * what had been said in that session — so a document with a hundred
 * comments on it looked like a document nobody had ever read.
 *
 * Grouping is the part that goes wrong without looking wrong. A flat
 * list in timestamp order renders as a rail full of comments; it is
 * only on reading one that you notice the answer is sitting under
 * somebody else's question.
 */
import { describe, expect, it } from "vitest";

import { threadsFrom, type StoredComment } from "./DocumentComment";

const row = (over: Partial<StoredComment> & { comment_id: string }): StoredComment => ({
  thread_id: "",
  parent_id: "",
  origin: "web",
  quote: "",
  body: "",
  status: "open",
  author: "sam@acme.test",
  at: "2026-09-01T10:00:00Z",
  n: 1,
  ...over,
});

describe("grouping stored comments into threads", () => {
  it("puts a reply under the comment it answers", () => {
    const threads = threadsFrom([
      row({ comment_id: "b", thread_id: "t1", parent_id: "a", body: "Because of the lock." }),
      row({ comment_id: "a", thread_id: "t1", body: "Why?" }),
    ]);

    expect(threads).toHaveLength(1);
    expect(threads[0].turns.map((t) => t.body)).toEqual(["Why?", "Because of the lock."]);
  });

  it("keeps separate threads separate", () => {
    const threads = threadsFrom([
      row({ comment_id: "a", thread_id: "t1" }),
      row({ comment_id: "b", thread_id: "t2" }),
    ]);

    expect(threads).toHaveLength(2);
  });

  it("does not collapse comments that have no thread id", () => {
    // Falling back to a shared empty string would file every one of
    // them as replies to whichever arrived first.
    const threads = threadsFrom([
      row({ comment_id: "a", body: "One" }),
      row({ comment_id: "b", body: "Two" }),
    ]);

    expect(threads).toHaveLength(2);
  });

  it("orders replies among themselves by when they were written", () => {
    const threads = threadsFrom([
      row({ comment_id: "c", thread_id: "t", parent_id: "a", body: "third", at: "2026-09-03T00:00:00Z" }),
      row({ comment_id: "a", thread_id: "t", body: "first", at: "2026-09-01T00:00:00Z" }),
      row({ comment_id: "b", thread_id: "t", parent_id: "a", body: "second", at: "2026-09-02T00:00:00Z" }),
    ]);

    expect(threads[0].turns.map((t) => t.body)).toEqual(["first", "second", "third"]);
  });

  it("puts the newest conversation at the top", () => {
    // A rail is read from the top, and the argument people are having
    // now is the one worth finding there.
    const threads = threadsFrom([
      row({ comment_id: "old", thread_id: "t1", at: "2026-01-01T00:00:00Z" }),
      row({ comment_id: "new", thread_id: "t2", at: "2026-09-01T00:00:00Z" }),
    ]);

    expect(threads.map((t) => t.id)).toEqual(["t2", "t1"]);
  });

  it("carries the thread's id, not the opening comment's", () => {
    // What a reply is posted against. The two are different strings —
    // the backend mints ``local-thread-…`` when a thread starts — and
    // sending the comment id back joined the reply to nothing, so it
    // was never recorded and never appeared.
    const threads = threadsFrom([
      row({ comment_id: "c-1", thread_id: "local-thread-abc" }),
    ]);

    expect(threads[0].id).toBe("local-thread-abc");
  });

  it("falls back to the comment id when there is no thread id", () => {
    // Older rows, and anything written before threads had ids. It
    // still has to be posted against something.
    const threads = threadsFrom([row({ comment_id: "c-1", thread_id: "" })]);

    expect(threads[0].id).toBe("c-1");
  });

  it("marks igni's turns as the agent's", () => {
    // A transcript, not an instruction. Rendering the agent's own
    // reply as a person's would put it up for answering again.
    const threads = threadsFrom([
      row({ comment_id: "a", thread_id: "t" }),
      row({ comment_id: "b", thread_id: "t", parent_id: "a", origin: "agent" }),
    ]);

    expect(threads[0].turns.map((t) => t.who)).toEqual(["me", "agent"]);
  });

  it("carries the version and the status of the opening comment", () => {
    const threads = threadsFrom([
      row({ comment_id: "a", thread_id: "t", n: 12, status: "accepted" }),
    ]);

    expect(threads[0].n).toBe(12);
    expect(threads[0].status).toBe("accepted");
  });

  it("names who wrote each turn", () => {
    // Several people in one thread. Without the names it reads as one
    // voice arguing with itself.
    const threads = threadsFrom([
      row({ comment_id: "a", thread_id: "t", author: "dana@acme.test" }),
      row({ comment_id: "b", thread_id: "t", parent_id: "a", author: "raj@acme.test" }),
    ]);

    expect(threads[0].turns.map((t) => t.author)).toEqual([
      "dana@acme.test",
      "raj@acme.test",
    ]);
  });

  it("survives a thread whose opening comment is missing", () => {
    // The root can be absent: a reply projected before its parent, or
    // a root deleted on the server. Dropping the replies would lose
    // the conversation silently.
    const threads = threadsFrom([
      row({ comment_id: "b", thread_id: "t", parent_id: "gone", body: "orphan" }),
    ]);

    expect(threads).toHaveLength(1);
    expect(threads[0].turns.map((t) => t.body)).toEqual(["orphan"]);
  });

  it("is empty for a document nobody has commented on", () => {
    expect(threadsFrom([])).toEqual([]);
  });
});
