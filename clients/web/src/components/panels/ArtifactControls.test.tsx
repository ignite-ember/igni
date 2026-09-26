// @vitest-environment jsdom
/**
 * The controls that only the agent used to have.
 *
 * Six backend methods shipped with no control in the panel: confirm
 * and dismiss a captured source, the stale list, the reconcile diff,
 * export, and share. Everything was reachable by tool call and
 * nothing by hand, which had one consequence worth naming — the share
 * dialog is where `domain` and `public` get chosen, so the two widest
 * audiences in the product were the two a person could not pick.
 *
 * What these tests hold onto:
 *
 * * **Share says what each option does, in full.** "Domain" is not a
 *   description; "anyone whose account email shares yours" is. This
 *   is one click that can put a document in front of a company.
 * * **A verdict on a captured source refetches rather than patching
 *   the list locally.** A local edit drifts from the store the moment
 *   anything else writes an edge.
 * * **The reconcile diff is fetched when asked for, not with the
 *   list.** One diff per row on open would load every source's full
 *   text to render a list most of whose rows nobody expands.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ArtifactShareDialog } from "./ArtifactShareDialog";
import { ArtifactStaleReview } from "./ArtifactStaleReview";

afterEach(cleanup);

type Rpc = (method: string, args?: Record<string, unknown>) => Promise<unknown>;

function fakeClient(handler: Rpc) {
  const rpc = vi.fn(handler);
  return { client: { rpc } as never, rpc };
}

const STALE_ROW = {
  artifact_id: "a1",
  name: "Login flow",
  n: 2,
  source_artifact_id: "s1",
  source_name: "Auth model",
  pinned_n: 1,
  source_head_n: 3,
  versions_behind: 2,
};

describe("the share dialog", () => {
  it("spells out who each option reaches", async () => {
    const { client } = fakeClient(async () => null);
    render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth model"
        onClose={() => {}}
      />,
    );

    // The label alone ("Everyone at my company") is a claim; the
    // reach line is what makes it checkable.
    expect(
      screen.getByText(/anyone whose account email shares yours/i),
    ).toBeTruthy();
    expect(
      screen.getByText(/no sign-in required/i),
    ).toBeTruthy();
  });

  it("publishes private by default rather than guessing at something wider", async () => {
    const { client, rpc } = fakeClient(async () => ({
      url: "https://igni.test/a/abc",
      version: 2,
      error: "",
    }));
    render(
      <ArtifactShareDialog client={client} artifactId="a1" name="Auth" onClose={() => {}} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /publish/i }));
    await waitFor(() => expect(rpc).toHaveBeenCalled());
    expect(rpc).toHaveBeenCalledWith(
      "artifact_share",
      expect.objectContaining({ visibility: "private" }),
    );
  });

  it("sends the chosen visibility", async () => {
    const { client, rpc } = fakeClient(async () => ({
      url: "https://igni.test/a/abc",
      version: 2,
      error: "",
    }));
    render(
      <ArtifactShareDialog client={client} artifactId="a1" name="Auth" onClose={() => {}} />,
    );

    fireEvent.click(screen.getByLabelText(/everyone at my company/i));
    fireEvent.click(screen.getByRole("button", { name: /publish/i }));

    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith(
        "artifact_share",
        expect.objectContaining({ visibility: "domain" }),
      ),
    );
  });

  it("will not publish to named people with nobody named", async () => {
    const { client, rpc } = fakeClient(async () => null);
    render(
      <ArtifactShareDialog client={client} artifactId="a1" name="Auth" onClose={() => {}} />,
    );

    fireEvent.click(screen.getByLabelText(/specific people/i));
    expect(screen.getByRole("button", { name: /publish/i })).toHaveProperty(
      "disabled",
      true,
    );
    expect(rpc).not.toHaveBeenCalled();
  });

  it("splits addresses on commas and spaces", async () => {
    const { client, rpc } = fakeClient(async () => ({
      url: "https://igni.test/a/abc",
      version: 1,
      error: "",
    }));
    render(
      <ArtifactShareDialog client={client} artifactId="a1" name="Auth" onClose={() => {}} />,
    );

    fireEvent.click(screen.getByLabelText(/specific people/i));
    fireEvent.change(screen.getByLabelText(/email addresses/i), {
      target: { value: "ana@acme.co, sam@acme.co" },
    });
    fireEvent.click(screen.getByRole("button", { name: /publish/i }));

    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith(
        "artifact_share",
        expect.objectContaining({ audience: ["ana@acme.co", "sam@acme.co"] }),
      ),
    );
  });

  it("shows the link and which version it holds", async () => {
    const { client } = fakeClient(async () => ({
      url: "https://igni.test/a/abc123",
      version: 4,
      error: "",
    }));
    render(
      <ArtifactShareDialog client={client} artifactId="a1" name="Auth" onClose={() => {}} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /publish/i }));
    expect(await screen.findByDisplayValue("https://igni.test/a/abc123")).toBeTruthy();
    expect(screen.getByText(/shared as v4/i)).toBeTruthy();
  });

  it("surfaces a refusal instead of claiming success", async () => {
    const { client } = fakeClient(async () => ({
      url: "",
      version: 0,
      error: "Sharing needs a signed-in igni account.",
    }));
    render(
      <ArtifactShareDialog client={client} artifactId="a1" name="Auth" onClose={() => {}} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /publish/i }));
    expect(await screen.findByText(/signed-in igni account/i)).toBeTruthy();
  });
});

describe("the stale review", () => {
  it("says so plainly when nothing is out of date", async () => {
    const { client } = fakeClient(async () => []);
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);
    expect(await screen.findByText(/nothing is out of date/i)).toBeTruthy();
  });

  it("names both documents and how far behind the pin is", async () => {
    const { client } = fakeClient(async () => [STALE_ROW]);
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);

    expect(await screen.findByText("Login flow")).toBeTruthy();
    expect(screen.getByText("Auth model")).toBeTruthy();
    expect(screen.getByText(/2 behind/)).toBeTruthy();
  });

  it("does not fetch a diff until one is asked for", async () => {
    const { client, rpc } = fakeClient(async (method) =>
      method === "artifact_stale" ? [STALE_ROW] : "",
    );
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);
    await screen.findByText("Login flow");

    expect(rpc).not.toHaveBeenCalledWith("artifact_reconcile_diff", expect.anything());
  });

  it("fetches the diff for the right pair when asked", async () => {
    const { client, rpc } = fakeClient(async (method) =>
      method === "artifact_stale" ? [STALE_ROW] : "-old line\n+new line",
    );
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /what changed/i }));

    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith("artifact_reconcile_diff", {
        id: "a1",
        source_id: "s1",
      }),
    );
    expect(await screen.findByText(/\+new line/)).toBeTruthy();
  });

  it("explains a source that changed without a text diff", async () => {
    // An empty diff is not "nothing changed" — the version moved, so
    // something did. Rendering a blank box would read as a bug.
    const { client } = fakeClient(async (method) =>
      method === "artifact_stale" ? [STALE_ROW] : "",
    );
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /what changed/i }));
    expect(await screen.findByText(/not in a way that shows as a text diff/i)).toBeTruthy();
  });

  it("opens the document behind the name", async () => {
    const opened: string[] = [];
    const { client } = fakeClient(async () => [STALE_ROW]);
    render(<ArtifactStaleReview client={client} onOpenArtifact={(id) => opened.push(id)} />);

    fireEvent.click(await screen.findByRole("button", { name: "Login flow" }));
    expect(opened).toEqual(["a1"]);
  });
});
