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

/** Somewhere to hang the popover. It is anchored to whatever opened
 *  it, and a test that passes null would render nothing at all. */
const ANCHOR = { left: 100, right: 400, top: 80, bottom: 100 };
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

/** What `artifact_compare` returns for `Auth model` v1 → v3. */
const STALE_DIFF = {
  from_n: 1,
  to_n: 3,
  added: 1,
  removed: 1,
  changed: 0,
  truncated: 0,
  rows: [
    {
      kind: "removed",
      left_n: 1,
      right_n: null,
      left: "Auth is a session cookie.",
      right: "",
      left_parts: [{ text: "Auth is a session cookie.", changed: false }],
      right_parts: [],
    },
    {
      kind: "added",
      left_n: null,
      right_n: 1,
      left: "",
      right: "Auth is a bearer token.",
      left_parts: [],
      right_parts: [{ text: "Auth is a bearer token.", changed: false }],
    },
  ],
};

/** The same call when the source moved without its text moving. */
const EMPTY_DIFF = {
  from_n: 1,
  to_n: 3,
  added: 0,
  removed: 0,
  changed: 0,
  truncated: 0,
  rows: [],
};

describe("the share dialog", () => {
  it("spells out who each option reaches", async () => {
    const { client } = fakeClient(async () => null);
    render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth model"
        at={ANCHOR}
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
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /save/i }));
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
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByLabelText(/everyone at my company/i));
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

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
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByLabelText(/specific people/i));
    expect(screen.getByRole("button", { name: /save/i })).toHaveProperty(
      "disabled",
      true,
    );
    expect(rpc).not.toHaveBeenCalled();
  });

  it("sends the people who were ticked", async () => {
    // The audience used to be a line of text somebody typed from
    // memory, which made "who can read this" a spelling test. It is
    // a list of people and a tick beside each now, so the thing
    // published is the thing on screen.
    const { client, rpc } = fakeClient(async (method) =>
      method === "artifact_colleagues"
        ? {
            people: [
              { user_id: "u1", email: "ana@acme.co", name: "Ana Diaz" },
              { user_id: "u2", email: "sam@acme.co", name: "Sam Ortega" },
            ],
          }
        : { url: "https://igni.test/a/abc", version: 1, error: "" },
    );
    render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByLabelText(/specific people/i));
    fireEvent.click(await screen.findByText("Ana Diaz"));
    fireEvent.click(await screen.findByText("Sam Ortega"));
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith(
        "artifact_share",
        expect.objectContaining({ audience: ["ana@acme.co", "sam@acme.co"] }),
      ),
    );
  });

  it("closes once the change is saved", async () => {
    // The panel used to stay open and relabel itself Done and Update
    // — four labels for two controls, reporting a state it no longer
    // needed to be open to show. The link is on the header, where it
    // is reachable without opening anything.
    const closed = vi.fn();
    const { client } = fakeClient(async () => ({
      url: "https://igni.test/a/abc",
      version: 3,
      error: "",
    }));
    render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        onClose={closed}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(closed).toHaveBeenCalled());
  });

  it("says what was saved, so the header can change with it", async () => {
    const shared = vi.fn();
    const { client } = fakeClient(async () => ({
      url: "https://igni.test/a/abc",
      version: 3,
      error: "",
    }));
    render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        reach="public"
        onShared={shared}
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(shared).toHaveBeenCalledWith("public", [], "https://igni.test/a/abc"),
    );
  });

  it("stays open on a refusal, with the reason", async () => {
    // A panel that closed on a refusal would report the change as
    // made.
    const closed = vi.fn();
    const { client } = fakeClient(async () => ({
      url: "",
      version: 0,
      error: "Refused.",
    }));
    render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        onClose={closed}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    expect(await screen.findByText("Refused.")).toBeTruthy();
    expect(closed).not.toHaveBeenCalled();
  });

  it("surfaces a refusal instead of claiming success", async () => {
    const { client } = fakeClient(async () => ({
      url: "",
      version: 0,
      error: "Sharing needs a signed-in igni account.",
    }));
    render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /save/i }));
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
    // Branch wording: where we branched, and where that branch is
    // now. "Sources have changed" said something had happened to us.
    expect(screen.getByText(/branched from/)).toBeTruthy();
    expect(screen.getByText(/2 versions behind/)).toBeTruthy();
  });

  it("does not fetch a diff until one is asked for", async () => {
    const { client, rpc } = fakeClient(async (method) =>
      method === "artifact_stale" ? [STALE_ROW] : "",
    );
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);
    await screen.findByText("Login flow");

    expect(rpc).not.toHaveBeenCalledWith("artifact_compare", expect.anything());
  });

  it("compares the source across the span the pin fell behind by", async () => {
    // Not a diff of *this* document: the question the row asks is
    // what moved in the source between the version this one was
    // written against and the version the source is at now.
    const { client, rpc } = fakeClient(async (method) =>
      method === "artifact_stale" ? [STALE_ROW] : STALE_DIFF,
    );
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /what landed/i }));

    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith("artifact_compare", {
        id: "s1",
        from_n: 1,
        to_n: 3,
      }),
    );
  });

  it("renders the change as a comparison, not as unified-diff text", async () => {
    const { client } = fakeClient(async (method) =>
      method === "artifact_stale" ? [STALE_ROW] : STALE_DIFF,
    );
    const { container } = render(
      <ArtifactStaleReview client={client} onOpenArtifact={() => {}} />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: /what landed/i }),
    );

    // Both sides present, side by side, each labelled with the
    // version it is — and no ``---``/``+++``/``@@`` header anywhere.
    expect(await screen.findByText("Auth is a session cookie.")).toBeTruthy();
    expect(screen.getByText("Auth is a bearer token.")).toBeTruthy();
    expect(container.querySelector(".artifact-diff")).toBeTruthy();
    expect(container.querySelector("pre")).toBeNull();
    expect(container.textContent).not.toMatch(/@@|\+\+\+|---/);
  });

  it("explains a source that changed without a text diff", async () => {
    // An empty diff is not "nothing changed" — the version moved, so
    // something did. Rendering a blank box would read as a bug.
    const { client } = fakeClient(async (method) =>
      method === "artifact_stale" ? [STALE_ROW] : EMPTY_DIFF,
    );
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /what landed/i }));
    expect(
      await screen.findByText(/not in a way that shows as a text change/i),
    ).toBeTruthy();
  });

  it("asks the agent nothing until it is asked to", async () => {
    // Every row in this queue would otherwise start a model run the
    // moment the page was looked at.
    const { client, rpc } = fakeClient(async (method) =>
      method === "artifact_stale" ? [STALE_ROW] : null,
    );
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);
    await screen.findByText("Login flow");

    expect(rpc).not.toHaveBeenCalledWith("artifact_drift", expect.anything());
  });

  it("gathers the drift for the right pair when asked", async () => {
    const { client, rpc } = fakeClient(async (method) =>
      method === "artifact_stale"
        ? [STALE_ROW]
        : { prompt: "", affected: [], error: "Nothing has diverged." },
    );
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /what does it mean/i }));

    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith("artifact_drift", { id: "a1", source_id: "s1" }),
    );
  });

  it("shows which of our sentences lean on what moved", async () => {
    // The evidence the verdict is made from, worth reading even if
    // the agent is never asked.
    const { client } = fakeClient(async (method) =>
      method === "artifact_stale"
        ? [STALE_ROW]
        : {
            prompt: "ASK",
            error: "",
            affected: [
              { text: "The flow stores the signed JWT.", line_from: 3, line_to: 3, strength: "close" },
            ],
          },
    );
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /what does it mean/i }));

    expect(await screen.findByText("The flow stores the signed JWT.")).toBeTruthy();
    expect(screen.getByText("close match")).toBeTruthy();
  });

  it("runs no turn when nothing has actually diverged", async () => {
    const { client } = fakeClient(async (method) =>
      method === "artifact_stale"
        ? [STALE_ROW]
        : { prompt: "", affected: [], error: "Nothing has diverged." },
    );
    const runMessage = vi.fn();
    (client as unknown as { runMessage: unknown }).runMessage = runMessage;
    render(<ArtifactStaleReview client={client} onOpenArtifact={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /what does it mean/i }));

    expect(await screen.findByText("Nothing has diverged.")).toBeTruthy();
    expect(runMessage).not.toHaveBeenCalled();
  });

  it("opens the document behind the name", async () => {
    const opened: string[] = [];
    const { client } = fakeClient(async () => [STALE_ROW]);
    render(<ArtifactStaleReview client={client} onOpenArtifact={(id) => opened.push(id)} />);

    fireEvent.click(await screen.findByRole("button", { name: "Login flow" }));
    expect(opened).toEqual(["a1"]);
  });
});

describe("the share panel opens on how the document is shared", () => {
  it("opens on private for a document nobody has shared", async () => {
    // The narrowest answer, and the right default for a decision this
    // consequential.
    const { client } = fakeClient(async () => ({ url: "", version: 0, error: "" }));
    render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        onClose={() => {}}
      />,
    );

    expect(
      (screen.getByRole("radio", { name: /only me/i }) as HTMLInputElement).checked,
    ).toBe(true);
  });

  it("opens on what the document actually is", async () => {
    // It opened on "Only me" whatever the document was. Survivable
    // while this was a page; wrong the moment it became a popover
    // hanging off a line that reads "Anyone with the link".
    const { client } = fakeClient(async () => ({ url: "", version: 0, error: "" }));
    render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        reach="public"
        onClose={() => {}}
      />,
    );

    expect(
      (screen.getByRole("radio", { name: /anyone with the link/i }) as HTMLInputElement)
        .checked,
    ).toBe(true);
  });

  it("brings the addresses it already names", async () => {
    // Otherwise publishing again from here drops everybody who was on
    // it, which is an unshare nobody asked for.
    const { client, rpc } = fakeClient(async () => ({ url: "u", version: 1, error: "" }));
    render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={ANCHOR}
        reach="users"
        listed={["dana@acme.test", "sam@acme.test"]}
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(rpc).toHaveBeenCalled());
    expect(rpc).toHaveBeenCalledWith(
      "artifact_share",
      expect.objectContaining({ audience: ["dana@acme.test", "sam@acme.test"] }),
    );
  });

  it("draws nothing with nothing to hang off", () => {
    const { client } = fakeClient(async () => ({ url: "", version: 0, error: "" }));
    const { container } = render(
      <ArtifactShareDialog
        client={client}
        artifactId="a1"
        name="Auth"
        at={null}
        onClose={() => {}}
      />,
    );

    expect(container.querySelector(".kb-share")).toBeNull();
  });
});
