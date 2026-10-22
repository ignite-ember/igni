// @vitest-environment jsdom
/**
 * The review queue.
 *
 * Two things here can do real damage and both are tested hardest.
 *
 * **Accepting writes a version.** The text that lands is whatever is
 * in the box, not whatever the commenter originally proposed — the
 * owner deciding a suggestion is *nearly* right is the common case,
 * and a screen that silently discarded their edit would write
 * something nobody approved.
 *
 * **A partial failure must not read as success.** The write and the
 * server acknowledgement are separate steps, and "version written,
 * server not told" is a state the person needs to hear about. The
 * panel reports the server's own message rather than a cheerful
 * refresh.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ArtifactCommentReview, groupByDocument } from "./ArtifactCommentReview";
import type { IgniClient } from "../../protocol/client";

afterEach(cleanup);

function comment(overrides: Record<string, unknown> = {}) {
  return {
    comment_id: "c1",
    artifact_id: "a1",
    document_name: "Auth model",
    n: 2,
    thread_id: "c1",
    quote: "session cookie",
    body: "This contradicts the JWT decision.",
    suggested_text: "",
    status: "open",
    resolved_in_n: 0,
    author: "colleague@acme.test",
    at: "2026-09-26T10:00:00Z",
    behind_head: false,
    head_n: 2,
    ...overrides,
  };
}

function makeClient(rows: ReturnType<typeof comment>[], resolved?: Record<string, unknown>) {
  const calls: { method: string; args: unknown }[] = [];
  const client = {
    rpc: vi.fn(async (method: string, args?: unknown) => {
      calls.push({ method, args });
      if (method === "artifact_comments") return rows;
      if (method === "artifact_sync_comments") return rows.length;
      if (method === "artifact_resolve_comment") {
        return resolved ?? { ok: true, status: "accepted", resolved_in_n: 3, message: "" };
      }
      return null;
    }),
  } as unknown as IgniClient;
  return { client, calls };
}

describe("ArtifactCommentReview", () => {
  it("shows what is waiting, grouped under its document", async () => {
    const { client } = makeClient([comment()]);
    render(<ArtifactCommentReview client={client} />);
    expect(await screen.findByText("Auth model")).toBeTruthy();
    expect(screen.getByText("This contradicts the JWT decision.")).toBeTruthy();
  });

  it("shows the quoted passage so the comment can be read in context", async () => {
    const { client } = makeClient([comment()]);
    render(<ArtifactCommentReview client={client} />);
    expect(await screen.findByText("session cookie")).toBeTruthy();
  });

  it("says which version the comment was made on", async () => {
    const { client } = makeClient([comment({ n: 2 })]);
    render(<ArtifactCommentReview client={client} />);
    expect(await screen.findByText(/on v2/)).toBeTruthy();
  });

  it("warns when the document has moved past the version quoted", async () => {
    // Acting on a passage that no longer appears is the mistake this
    // screen is most likely to invite.
    const { client } = makeClient([comment({ behind_head: true })]);
    render(<ArtifactCommentReview client={client} />);
    expect(await screen.findByText(/moved on since/)).toBeTruthy();
  });

  it("says plainly when nothing is waiting", async () => {
    const { client } = makeClient([]);
    render(<ArtifactCommentReview client={client} />);
    expect(await screen.findByText(/Nothing is waiting on you/)).toBeTruthy();
  });

  it("offers no Accept button when there is nothing to apply", async () => {
    // A plain comment has no text to write, so accepting it would
    // have to invent one.
    const { client } = makeClient([comment({ suggested_text: "" })]);
    render(<ArtifactCommentReview client={client} />);
    await screen.findByText("Auth model");
    expect(screen.queryByRole("button", { name: /Accept/ })).toBeNull();
  });

  it("accepting sends the suggested text and the accepted status", async () => {
    const { client, calls } = makeClient([
      comment({ suggested_text: "Auth is a signed JWT." }),
    ]);
    render(<ArtifactCommentReview client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: /Accept/ }));

    await waitFor(() => {
      expect(calls.some((c) => c.method === "artifact_resolve_comment")).toBe(true);
    });
    const call = calls.find((c) => c.method === "artifact_resolve_comment")!;
    expect(call.args).toMatchObject({
      comment_id: "c1",
      status: "accepted",
      content: "Auth is a signed JWT.",
    });
  });

  it("accepting writes the owner's edit, not the original suggestion", async () => {
    const { client, calls } = makeClient([
      comment({ suggested_text: "Auth is a signed JWT." }),
    ]);
    render(<ArtifactCommentReview client={client} />);
    const box = (await screen.findByLabelText(
      "Suggested replacement for c1",
    )) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "Auth is a 15-minute JWT." } });
    fireEvent.click(screen.getByRole("button", { name: /Accept/ }));

    await waitFor(() => {
      expect(calls.some((c) => c.method === "artifact_resolve_comment")).toBe(true);
    });
    const call = calls.find((c) => c.method === "artifact_resolve_comment")!;
    expect(call.args).toMatchObject({ content: "Auth is a 15-minute JWT." });
  });

  it("declining sends no content at all", async () => {
    // Rejecting must not write a version. A rejected comment is the
    // record of a decision, not an edit.
    const { client, calls } = makeClient([
      comment({ suggested_text: "Auth is a signed JWT." }),
    ]);
    render(<ArtifactCommentReview client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "Decline" }));

    await waitFor(() => {
      expect(calls.some((c) => c.method === "artifact_resolve_comment")).toBe(true);
    });
    const call = calls.find((c) => c.method === "artifact_resolve_comment")!;
    expect(call.args).toMatchObject({ status: "rejected" });
    expect((call.args as Record<string, unknown>).content).toBeUndefined();
  });

  it("resolving sends no content either", async () => {
    const { client, calls } = makeClient([comment()]);
    render(<ArtifactCommentReview client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "Mark resolved" }));

    await waitFor(() => {
      expect(calls.some((c) => c.method === "artifact_resolve_comment")).toBe(true);
    });
    const call = calls.find((c) => c.method === "artifact_resolve_comment")!;
    expect(call.args).toMatchObject({ status: "resolved" });
  });

  it("a partial failure is reported, not swallowed", async () => {
    // "Version 3 was written, but the server was not told" is a state
    // the person has to hear about — the next sync will not repair it.
    const { client } = makeClient([comment({ suggested_text: "x" })], {
      ok: false,
      status: "accepted",
      resolved_in_n: 3,
      message: "Version 3 was written, but the server was not told: timeout",
    });
    render(<ArtifactCommentReview client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: /Accept/ }));
    expect(await screen.findByText(/server was not told/)).toBeTruthy();
  });

  it("reloads after acting so the queue reflects what happened", async () => {
    const { client, calls } = makeClient([comment()]);
    render(<ArtifactCommentReview client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "Mark resolved" }));

    await waitFor(() => {
      const loads = calls.filter((c) => c.method === "artifact_comments");
      expect(loads.length).toBeGreaterThan(1);
    });
  });

  it("checking for new pulls from the server then reloads", async () => {
    const { client, calls } = makeClient([comment()]);
    render(<ArtifactCommentReview client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "Check for new" }));

    await waitFor(() => {
      expect(calls.some((c) => c.method === "artifact_sync_comments")).toBe(true);
    });
  });

  it("asks only for what is still open", async () => {
    const { client, calls } = makeClient([]);
    render(<ArtifactCommentReview client={client} />);
    await waitFor(() => {
      expect(calls.some((c) => c.method === "artifact_comments")).toBe(true);
    });
    expect(calls[0].args).toMatchObject({ open_only: true });
  });

  it("surfaces a load failure rather than showing an empty queue", async () => {
    // An empty list and a broken connection look identical, and one
    // of them means "you are done for the day".
    const client = {
      rpc: vi.fn(async () => {
        throw new Error("backend is down");
      }),
    } as unknown as IgniClient;
    render(<ArtifactCommentReview client={client} />);
    expect(await screen.findByText(/backend is down/)).toBeTruthy();
  });
});

describe("groupByDocument", () => {
  it("keeps arrival order, so the longest wait stays on top", () => {
    const grouped = groupByDocument([
      comment({ comment_id: "a", document_name: "Zeta" }),
      comment({ comment_id: "b", document_name: "Alpha" }),
      comment({ comment_id: "c", document_name: "Zeta" }),
    ]);
    expect(grouped.map(([name, rows]) => [name, rows.length])).toEqual([
      ["Zeta", 2],
      ["Alpha", 1],
    ]);
  });
});

describe("the version an accepted suggestion writes", () => {
  it("counts from head, not from the version commented on", () => {
    // Accepting revises the document, and a revision appends after
    // whatever is current. A comment left on v2 of a document since
    // taken to v5 used to offer "Accept and write v3" and write v6 —
    // a button wrong about what it writes to the store.
    const { client } = makeClient([
      comment({ n: 2, head_n: 5, suggested_text: "the replacement", behind_head: true }),
    ]);
    render(<ArtifactCommentReview client={client} />);

    return screen.findByRole("button", { name: "Accept and write v6" });
  });

  it("falls back to the comment's own version when head is unknown", () => {
    // A comment can outlive the document it was left on, and the
    // projection then has no head to report. Offering "v1" there
    // would be a worse guess than the one fact we still have.
    const { client } = makeClient([
      comment({ n: 4, head_n: 0, suggested_text: "the replacement" }),
    ]);
    render(<ArtifactCommentReview client={client} />);

    return screen.findByRole("button", { name: "Accept and write v5" });
  });
});
