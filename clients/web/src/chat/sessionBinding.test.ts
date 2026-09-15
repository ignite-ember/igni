/**
 * Tests for ``planSessionBinding`` — which session a window adopts
 * when it connects.
 *
 * The two cases worth pinning are the ones that only show up over
 * time: a reload (the ``?dir=`` param is still in the URL, so "dir
 * means bind a folder" would mint a new session on every refresh),
 * and a window label reused across app runs (labels are lowest-free,
 * so this run's ``w-2`` inherits last run's ``w-2`` client state —
 * which may belong to a completely different repo).
 */

import { describe, expect, it } from "vitest";

import { planSessionBinding } from "./sessionBinding";

describe("planSessionBinding", () => {
  it("takes the backend default when nothing is known", () => {
    // First window, first launch.
    expect(
      planSessionBinding({ wantedDir: "", storedSession: "", storedDir: "" }),
    ).toEqual({ kind: "default" });
  });

  it("resumes a stored session when the window has no folder", () => {
    // The pre-multi-window path: one window, reload, come back to
    // the same chat.
    expect(
      planSessionBinding({ wantedDir: "", storedSession: "abc123", storedDir: "" }),
    ).toEqual({ kind: "resume", sessionId: "abc123" });
  });

  it("binds the folder on a new window's first load", () => {
    expect(
      planSessionBinding({
        wantedDir: "/repos/beta",
        storedSession: "",
        storedDir: "",
      }),
    ).toEqual({ kind: "bind-folder", projectDir: "/repos/beta" });
  });

  it("resumes in place when the window reloads", () => {
    // The ``dir`` param survives the reload. Without this branch
    // every refresh would create another session in that folder.
    expect(
      planSessionBinding({
        wantedDir: "/repos/beta",
        storedSession: "abc123",
        storedDir: "/repos/beta",
      }),
    ).toEqual({
      kind: "resume-in-folder",
      sessionId: "abc123",
      projectDir: "/repos/beta",
    });
  });

  it("ignores a stored session from a different folder", () => {
    // Last run's ``w-2`` was opened on /repos/alpha; this run's
    // ``w-2`` was opened on /repos/beta. Adopting the stored session
    // would show alpha in a window the user pointed at beta.
    expect(
      planSessionBinding({
        wantedDir: "/repos/beta",
        storedSession: "abc123",
        storedDir: "/repos/alpha",
      }),
    ).toEqual({ kind: "bind-folder", projectDir: "/repos/beta" });
  });

  it("ignores a stored session with no recorded folder", () => {
    // Pre-multi-window state, or a client_state write that never
    // landed. Unknown provenance is not the same as "belongs here".
    expect(
      planSessionBinding({
        wantedDir: "/repos/beta",
        storedSession: "abc123",
        storedDir: "",
      }),
    ).toEqual({ kind: "bind-folder", projectDir: "/repos/beta" });
  });
});
