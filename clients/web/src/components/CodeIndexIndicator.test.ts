/**
 * Tests for the pure helpers behind ``CodeIndexIndicator``:
 *   • providerName — derives "GitHub" / "GitLab" / "Bitbucket"
 *     from a remote URL, with a generic fallback. Used in the
 *     "uninstalled" tooltip ("GitHub repository not connected").
 *   • classify     — picks one of six BadgeState rows based on a
 *     priority order. Drift in that order means the status pill
 *     shows the wrong state at the wrong time.
 *
 * The polling/effect side of the component isn't tested here —
 * it spins network state we don't mock. The pill copy + tone IS
 * the user-facing affordance, and classify() owns it.
 */

import { describe, expect, it } from "vitest";
import {
  classify,
  providerName,
  type CodeIndexStatus,
} from "./CodeIndexIndicator";

// ── providerName ─────────────────────────────────────────────

describe("providerName", () => {
  it("returns 'GitHub' for github.com URLs", () => {
    expect(providerName("https://github.com/anthropics/claude-code")).toBe(
      "GitHub",
    );
  });

  it("returns 'GitLab' for gitlab URLs", () => {
    expect(providerName("https://gitlab.com/group/repo")).toBe("GitLab");
    // Self-hosted GitLab too — host contains "gitlab".
    expect(providerName("https://gitlab.internal.example/team/repo")).toBe(
      "GitLab",
    );
  });

  it("returns 'Bitbucket' for bitbucket URLs", () => {
    expect(providerName("https://bitbucket.org/team/repo")).toBe("Bitbucket");
  });

  it("matches SSH-style remotes", () => {
    // git@github.com:user/repo.git is the cloned-via-SSH form.
    // The regex covers both http(s) and ``git@`` prefixes.
    expect(providerName("git@github.com:user/repo.git")).toBe("GitHub");
    expect(providerName("git@gitlab.com:user/repo.git")).toBe("GitLab");
  });

  it("is case-insensitive on the host", () => {
    // Mixed-case URLs happen (Bitbucket export, manual config).
    // The matcher lowercases before checking.
    expect(providerName("https://GitHub.com/x/y")).toBe("GitHub");
  });

  it("falls back to 'Git provider' for unknown hosts", () => {
    // Self-hosted Gitea, sourcehut, anything unrecognised. The
    // tooltip becomes "Git provider repository not connected" —
    // generic but truthful.
    expect(providerName("https://git.sr.ht/~user/repo")).toBe("Git provider");
    expect(providerName("")).toBe("Git provider");
  });
});

// ── classify ─────────────────────────────────────────────────
//
// The classifier is a priority chain — earlier checks override
// later ones. Changing the order has user-visible consequences:
// if ``sync_error`` is checked AFTER ``head_indexed``, an error
// during reindex would show "indexed" with a stale-good tone.
// Pin each priority transition.

const base: CodeIndexStatus = {
  head_indexed: false,
  sync_in_progress: false,
  sync_progress_pct: null,
  sync_error: "",
  install_state: "active",
  remote_url: "https://github.com/x/y",
};

describe("classify — null/loading", () => {
  it("returns the 'checking…' loading state for null status", () => {
    // Initial render before the first RPC lands. ``checking…``
    // tells the user "I haven't given up; I just don't know yet".
    const b = classify(null);
    expect(b.label).toBe("checking…");
    expect(b.tone).toBe("muted");
  });
});

describe("classify — priority order (each row pins one transition)", () => {
  it("sync_error wins over everything (including head_indexed)", () => {
    // Mid-reindex error → must NOT show "indexed". Pin this
    // because the priority drift is the silent-regression
    // scenario.
    const b = classify({
      ...base,
      sync_error: "oops",
      head_indexed: true,
      sync_in_progress: true,
    });
    expect(b.label).toBe("error");
    expect(b.tone).toBe("bad");
    expect(b.detail).toBe("oops");
  });

  it("needs_install wins over inactive + indexed", () => {
    // Repo not connected to the provider → the user can't
    // "just sync" their way out; they have to install. Surface
    // this above everything except an active error.
    const b = classify({
      ...base,
      install_state: "needs_install",
      head_indexed: true,
    });
    expect(b.label).toBe("uninstalled");
    expect(b.tone).toBe("warn");
    expect(b.detail).toMatch(/GitHub repository not connected/);
  });

  it("inactive shows the dedicated muted label", () => {
    const b = classify({ ...base, install_state: "inactive" });
    expect(b.label).toBe("inactive");
    expect(b.tone).toBe("muted");
  });

  it("sync_in_progress with no pct shows ellipsis", () => {
    // Early in a reindex, the BE may not have emitted a pct
    // yet. The ellipsis reads as "running, no number".
    const b = classify({ ...base, sync_in_progress: true });
    expect(b.label).toBe("syncing…");
    expect(b.tone).toBe("warn");
  });

  it("sync_in_progress with a pct shows it inline", () => {
    const b = classify({
      ...base,
      sync_in_progress: true,
      sync_progress_pct: 42,
    });
    expect(b.label).toBe("syncing 42%");
  });

  it("head_indexed (after all the warn states are clear) → 'indexed' / good", () => {
    const b = classify({ ...base, head_indexed: true });
    expect(b.label).toBe("indexed");
    expect(b.tone).toBe("good");
  });

  it("active install, not syncing, not indexed → 'not indexed' / warn", () => {
    // Final fallback — the install is wired, no sync running,
    // but the latest HEAD hasn't been indexed yet. User should
    // sync.
    const b = classify(base);
    expect(b.label).toBe("not indexed");
    expect(b.tone).toBe("warn");
  });

  it("sync_in_progress beats head_indexed (mid-resync of a stale index)", () => {
    // A common state — the index is technically valid but a
    // resync is running for a newer HEAD. Show the running
    // state, not the stale-good one.
    const b = classify({
      ...base,
      head_indexed: true,
      sync_in_progress: true,
      sync_progress_pct: 10,
    });
    expect(b.label).toBe("syncing 10%");
  });
});
