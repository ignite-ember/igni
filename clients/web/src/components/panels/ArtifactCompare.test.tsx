// @vitest-environment jsdom
/**
 * Comparing two versions.
 *
 * The rows arrive aligned from the backend, so what this tests is the
 * rendering decisions that carry meaning: that a changed line shows
 * both halves on one row, that the empty half of an unpaired change is
 * visibly absent rather than blank, and that "nothing changed" is said
 * out loud instead of shown as an empty panel.
 *
 * The default pair matters too. Somebody opening a comparison nearly
 * always means "what changed in the last revision", and a picker that
 * opened on v1-against-v1 would make them set it up every time.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ArtifactCompare } from "./ArtifactCompare";
import type { IgniClient } from "../../protocol/client";

afterEach(cleanup);

function row(over: Record<string, unknown> = {}) {
  return { kind: "equal", left_n: 1, left: "same", right_n: 1, right: "same", ...over };
}

function diff(over: Record<string, unknown> = {}) {
  return {
    artifact_id: "a1",
    name: "Login flow",
    from_n: 2,
    to_n: 3,
    rows: [row()],
    added: 0,
    removed: 0,
    changed: 0,
    truncated: 0,
    ...over,
  };
}

function makeClient(result: Record<string, unknown>) {
  const calls: { method: string; args: unknown }[] = [];
  const client = {
    rpc: vi.fn(async (method: string, args?: unknown) => {
      calls.push({ method, args });
      return result;
    }),
  } as unknown as IgniClient;
  return { client, calls };
}

describe("ArtifactCompare", () => {
  it("asks for the previous version against the current one", async () => {
    // "What changed in the last revision" is what opening this means.
    const { client, calls } = makeClient(diff());
    render(
      <ArtifactCompare
        client={client}
        artifactId="a1"
        versions={[3, 2, 1]}
        headN={3}
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    expect(calls[0].args).toMatchObject({ id: "a1", from_n: 2, to_n: 3 });
  });

  it("shows a rewritten line as one row with both halves", async () => {
    const { client } = makeClient(
      diff({
        rows: [
          row({
            kind: "changed",
            left: "good for 24 hours",
            right: "expires in 15 minutes",
          }),
        ],
        changed: 1,
      }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    await screen.findByText("good for 24 hours");

    const tr = document.querySelector('tr[data-kind="changed"]')!;
    expect(tr.textContent).toContain("good for 24 hours");
    expect(tr.textContent).toContain("expires in 15 minutes");
  });

  it("marks an addition so only the right half is filled", async () => {
    const { client } = makeClient(
      diff({
        rows: [row({ kind: "added", left_n: 0, left: "", right_n: 4, right: "new line" })],
        added: 1,
      }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    await screen.findByText("new line");

    const cells = document.querySelectorAll('tr[data-kind="added"] td');
    // left gutter, left cell, right gutter, right cell
    expect(cells[0].textContent).toBe("");
    expect(cells[1].textContent).toBe("");
    expect(cells[3].textContent).toBe("new line");
  });

  it("marks a removal so only the left half is filled", async () => {
    const { client } = makeClient(
      diff({
        rows: [row({ kind: "removed", left_n: 2, left: "gone", right_n: 0, right: "" })],
        removed: 1,
      }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    await screen.findByText("gone");
    expect(document.querySelectorAll('tr[data-kind="removed"] td')[3].textContent).toBe("");
  });

  it("says nothing changed rather than showing an empty panel", async () => {
    // A revision that only touched metadata produces an empty diff.
    const { client } = makeClient(diff({ rows: [], added: 0, removed: 0, changed: 0 }));
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText(/Nothing changed between v2 and v3/)).toBeTruthy();
  });

  it("counts a rewrite on both sides of the tally", async () => {
    // One changed line is one line gone and one arrived; a tally that
    // showed +0 −0 for a rewritten paragraph would be lying.
    const { client } = makeClient(
      diff({ rows: [row({ kind: "changed" })], changed: 2, added: 1, removed: 1 }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText("+3")).toBeTruthy();
    expect(screen.getByText("−3")).toBeTruthy();
  });

  it("re-asks when the pair changes", async () => {
    const { client, calls } = makeClient(diff());
    render(
      <ArtifactCompare
        client={client}
        artifactId="a1"
        versions={[3, 2, 1]}
        headN={3}
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(calls.length).toBe(1));

    const selects = document.querySelectorAll("select");
    fireEvent.change(selects[0], { target: { value: "1" } });
    await waitFor(() => expect(calls.length).toBe(2));
    expect(calls[1].args).toMatchObject({ from_n: 1, to_n: 3 });
  });

  it("says how much it left out", async () => {
    // A diff that quietly stops reads as "and nothing else changed".
    const { client } = makeClient(
      diff({ rows: [row({ kind: "added" })], added: 1, truncated: 40 }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText(/40 more lines not shown/)).toBeTruthy();
  });

  it("surfaces a failure instead of looking like no changes", async () => {
    const client = {
      rpc: vi.fn(async () => {
        throw new Error("backend is down");
      }),
    } as unknown as IgniClient;
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText(/backend is down/)).toBeTruthy();
  });

  it("marks which side is current", async () => {
    const { client } = makeClient(diff({ rows: [row({ kind: "added" })], added: 1 }));
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText(/v3 · current/)).toBeTruthy();
  });
});
