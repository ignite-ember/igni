// @vitest-environment jsdom
/**
 * A link is not the same as opening igni with nothing to open.
 *
 * The sign-in wall is right for the second and wrong for the first: a
 * public document was published to be read, and demanding an account
 * on the *sender's* self-hosted server before a recipient may read it
 * makes the link not a link.
 *
 * Read, and nothing else. The tree, the search, the composer and the
 * comments belong to people with access, and most of them have
 * nothing to act on for somebody with no account anyway.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { PublicDocument } from "./PublicDocument";

afterEach(cleanup);

function backend(answer: Record<string, unknown>) {
  const calls: { method: string; args: Record<string, unknown> }[] = [];
  return {
    calls,
    client: {
      rpc: vi.fn(async (method: string, args: Record<string, unknown>) => {
        calls.push({ method, args });
        return answer;
      }),
      onEvent: () => () => {},
    } as never,
  };
}

const DOC = {
  ok: "1",
  title: "Acme roadmap",
  content: "# Acme roadmap\n\nQ1: ship it.\n\n- one\n- two",
};

describe("reading a document from a link", () => {
  it("asks for it by slug", async () => {
    const { client, calls } = backend(DOC);

    render(
      <PublicDocument client={client} slug="abc123" onSignIn={() => {}} />,
    );

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      method: "public_document",
      args: { slug: "abc123" },
    });
  });

  it("renders the document", async () => {
    const { client } = backend(DOC);

    render(
      <PublicDocument client={client} slug="abc123" onSignIn={() => {}} />,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Acme roadmap" }),
      ).toBeTruthy(),
    );
    expect(screen.getByText("Q1: ship it.")).toBeTruthy();
  });

  it("says the copy is shared and read only", async () => {
    // Somebody should know they are seeing a shared document rather
    // than their own workspace.
    const { client } = backend(DOC);

    render(
      <PublicDocument client={client} slug="abc123" onSignIn={() => {}} />,
    );

    await waitFor(() => expect(screen.getByText(/read only/i)).toBeTruthy());
  });

  it("offers a way in without demanding one", async () => {
    const signIn = vi.fn();
    const { client } = backend(DOC);

    render(<PublicDocument client={client} slug="abc123" onSignIn={signIn} />);
    await waitFor(() => screen.getByRole("button", { name: /sign in/i }));

    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    expect(signIn).toHaveBeenCalled();
  });
});

describe("when the link does not open", () => {
  it("says so, and offers the thing that might fix it", async () => {
    // It may be shared with them by name rather than publicly, in
    // which case signing in is exactly what opens it.
    const { client } = backend({
      error:
        "That link did not open. It may be private, or it may have been revoked.",
    });

    render(
      <PublicDocument client={client} slug="abc123" onSignIn={() => {}} />,
    );

    await waitFor(() =>
      expect(screen.getByText(/may be private/)).toBeTruthy(),
    );
    expect(
      screen.getByRole("button", { name: /sign in and try again/i }),
    ).toBeTruthy();
  });

  it("shows a backend that threw rather than a blank page", async () => {
    const client = {
      rpc: vi.fn(async () => {
        throw new Error("Could not reach the server: ReadError");
      }),
      onEvent: () => () => {},
    } as never;

    render(
      <PublicDocument client={client} slug="abc123" onSignIn={() => {}} />,
    );

    await waitFor(() =>
      expect(screen.getByText(/Could not reach the server/)).toBeTruthy(),
    );
  });

  it("does not render a document it never received", async () => {
    const { client } = backend({ error: "no" });

    render(
      <PublicDocument client={client} slug="abc123" onSignIn={() => {}} />,
    );

    await waitFor(() => screen.getByText(/did not open/i));
    expect(document.querySelector(".pubdoc-body")).toBeNull();
  });
});
