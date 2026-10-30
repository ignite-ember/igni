// @vitest-environment jsdom
/**
 * The wall asks for an email, because that is what people have.
 *
 * "Paste your igni server address" asks for something most people do
 * not know. The domain in their address is already the name of the
 * organisation whose igni they should reach, and the organisation
 * publishes where it is — so the address answers the question, and
 * the server field is the fallback rather than the opening move.
 *
 * Most domains publish nothing, and that is not a failure. What
 * matters is that the fallback explains itself: an administrator who
 * reads "acme.example has no igni record published" knows what to go
 * and do, and one who reads "something went wrong" does not.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { SignInWall } from "./SignInWall";

afterEach(cleanup);

/** A backend whose discovery answers however the test says. */
function backend(answers: Record<string, unknown>) {
  const calls: { method: string; args: Record<string, unknown> }[] = [];
  return {
    calls,
    client: {
      rpc: vi.fn(async (method: string, args: Record<string, unknown>) => {
        calls.push({ method, args });
        return answers[method] ?? {};
      }),
      onEvent: () => () => {},
    } as never,
  };
}

async function typeEmail(value: string) {
  fireEvent.change(screen.getByLabelText("Work email"), {
    target: { value },
  });
  fireEvent.click(screen.getByRole("button", { name: /continue/i }));
}

describe("finding the server from an address", () => {
  it("asks for an email first, not a URL", () => {
    const { client } = backend({});

    render(<SignInWall client={client} onSignIn={() => {}} />);

    expect(screen.getByLabelText("Work email")).toBeTruthy();
    expect(screen.queryByLabelText("igni server address")).toBeNull();
  });

  it("looks the domain up and names what it found", async () => {
    const { client, calls } = backend({
      discover_server: {
        api_url: "https://igni.acme.example",
        source: "well-known",
      },
      set_server: { api_url: "https://igni.acme.example" },
    });

    render(<SignInWall client={client} onSignIn={() => {}} />);
    await typeEmail("sam@acme.example");

    // Named, so nobody signs in to the wrong company's deployment
    // without noticing.
    await waitFor(() =>
      expect(screen.getByText("igni.acme.example")).toBeTruthy(),
    );
    expect(calls[0]).toEqual({
      method: "discover_server",
      args: { email: "sam@acme.example" },
    });
  });

  it("writes the server down before offering to sign in", async () => {
    // The sign-in flow reads the configured server, so an unsaved one
    // would send somebody to whatever was there before.
    const { client, calls } = backend({
      discover_server: {
        api_url: "https://igni.acme.example",
        portal_url: "https://portal.acme.example",
      },
      set_server: { api_url: "https://igni.acme.example" },
    });

    render(<SignInWall client={client} onSignIn={() => {}} />);
    await typeEmail("sam@acme.example");

    await waitFor(() =>
      expect(calls.map((c) => c.method)).toEqual([
        "discover_server",
        "set_server",
      ]),
    );
    expect(calls[1].args).toEqual({
      api_url: "https://igni.acme.example",
      portal_url: "https://portal.acme.example",
    });
  });

  it("hands off to the deployment's own sign-in", async () => {
    // Sign-in is not reimplemented here: the deployment's page already
    // renders its own methods — password, its providers, its SSO
    // button under the name its administrator chose.
    const started = vi.fn();
    const { client } = backend({
      discover_server: { api_url: "https://igni.acme.example" },
      set_server: { api_url: "https://igni.acme.example" },
    });

    render(<SignInWall client={client} onSignIn={started} />);
    await typeEmail("sam@acme.example");
    await waitFor(() => screen.getByRole("button", { name: /^sign in$/i }));

    fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    expect(started).toHaveBeenCalled();
  });
});

describe("when the domain publishes nothing", () => {
  it("asks for the server and says why it is asking", async () => {
    const { client } = backend({
      discover_server: {
        reason: "acme.example has no igni record published.",
      },
    });

    render(<SignInWall client={client} onSignIn={() => {}} />);
    await typeEmail("sam@acme.example");

    await waitFor(() =>
      expect(screen.getByLabelText("igni server address")).toBeTruthy(),
    );
    // The reason, not a shrug — this is the sentence that tells an
    // administrator what to publish.
    expect(
      screen.getByText(/acme\.example has no igni record published/),
    ).toBeTruthy();
  });

  it("accepts a server typed by hand", async () => {
    const { client, calls } = backend({
      discover_server: { reason: "nothing published" },
      set_server: { api_url: "https://igni.acme.example" },
    });

    render(<SignInWall client={client} onSignIn={() => {}} />);
    await typeEmail("sam@acme.example");
    await waitFor(() => screen.getByLabelText("igni server address"));

    fireEvent.change(screen.getByLabelText("igni server address"), {
      target: { value: "https://igni.acme.example" },
    });
    fireEvent.click(screen.getByRole("button", { name: /connect/i }));

    await waitFor(() =>
      expect(screen.getByText("igni.acme.example")).toBeTruthy(),
    );
    expect(calls.at(-1)).toEqual({
      method: "set_server",
      args: { api_url: "https://igni.acme.example" },
    });
  });

  it("lets somebody go back and try another address", async () => {
    const { client } = backend({ discover_server: { reason: "nothing" } });

    render(<SignInWall client={client} onSignIn={() => {}} />);
    await typeEmail("sam@acme.example");
    await waitFor(() => screen.getByLabelText("igni server address"));

    fireEvent.click(screen.getByRole("button", { name: /different address/i }));

    expect(screen.getByLabelText("Work email")).toBeTruthy();
  });
});

describe("when something is refused", () => {
  it("shows the refusal and stays put", async () => {
    // A plaintext address is refused by the backend. The wall must
    // not advance as though it had been accepted.
    const { client } = backend({
      discover_server: { reason: "nothing published" },
      set_server: {
        error: "http://igni.acme.example is not an https address.",
      },
    });

    render(<SignInWall client={client} onSignIn={() => {}} />);
    await typeEmail("sam@acme.example");
    await waitFor(() => screen.getByLabelText("igni server address"));

    fireEvent.change(screen.getByLabelText("igni server address"), {
      target: { value: "http://igni.acme.example" },
    });
    fireEvent.click(screen.getByRole("button", { name: /connect/i }));

    await waitFor(() =>
      expect(screen.getByText(/is not an https address/)).toBeTruthy(),
    );
    expect(screen.getByLabelText("igni server address")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^sign in$/i })).toBeNull();
  });

  it("shows a backend that threw rather than swallowing it", async () => {
    const client = {
      rpc: vi.fn(async () => {
        throw new Error("Could not reach the server: ReadError");
      }),
      onEvent: () => () => {},
    } as never;

    render(<SignInWall client={client} onSignIn={() => {}} />);
    await typeEmail("sam@acme.example");

    await waitFor(() =>
      expect(screen.getByText(/Could not reach the server/)).toBeTruthy(),
    );
  });

  it("does not look anything up for an empty address", async () => {
    const { client, calls } = backend({});

    render(<SignInWall client={client} onSignIn={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));

    expect(calls).toEqual([]);
  });
});
