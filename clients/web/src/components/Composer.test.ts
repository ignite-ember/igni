/**
 * Tests for the Composer's slash-command surface — the parts
 * that don't need the contenteditable + state machine:
 *
 *   • BUILTIN_COMMANDS — structural contract (the FE call sites
 *     trust this list to be well-formed and to include the
 *     commands the TUI's CommandHandler exposes).
 *   • filterSlashCommands — the pure prefix-filter helper the
 *     ``/`` autocomplete menu uses. Extracted from refreshMenu
 *     so we don't have to drive the contenteditable to test it.
 *
 * The full Composer component (mention menu, history, mode
 * machine, etc.) is left to manual / E2E verification — it owns
 * too much DOM-bound state to be useful as a unit test.
 */

import { describe, expect, it } from "vitest";
import {
  BUILTIN_COMMANDS,
  filterSlashCommands,
  type SlashCommand,
} from "./Composer";

describe("BUILTIN_COMMANDS — structural contract", () => {
  it("is non-empty (the built-in slash commands must exist)", () => {
    expect(BUILTIN_COMMANDS.length).toBeGreaterThan(0);
  });

  it("every command name starts with '/'", () => {
    // The autocomplete menu strips the leading slash for the
    // prefix match; anything missing it would never appear in
    // the menu (the user types ``/`` and the filter walks
    // ``name.slice(1)``).
    for (const c of BUILTIN_COMMANDS) {
      expect(c.name.startsWith("/")).toBe(true);
    }
  });

  it("every command has a non-empty description", () => {
    // The description is the second column in the menu. An
    // empty description renders as a blank line — useless to
    // the user and a hint that the entry was added carelessly.
    for (const c of BUILTIN_COMMANDS) {
      expect(c.description.length).toBeGreaterThan(0);
    }
  });

  it("command names are unique (no duplicates)", () => {
    // Two ``/help`` entries would put the same command twice in
    // the menu and double-route on Enter. Catch the duplicate
    // at structural-test time.
    const names = BUILTIN_COMMANDS.map((c) => c.name);
    expect(new Set(names).size).toBe(names.length);
  });

  it("ships every BE command handler in the autocomplete menu", () => {
    // ALL non-alias entries from ``CommandHandler._COMMANDS`` in
    // ``src/ember_code/backend/command_handler.py`` must appear in
    // ``BUILTIN_COMMANDS`` — otherwise a user types ``/x`` and the
    // menu silently omits it even though the BE handles it. This
    // list is the single source of truth; when a new handler is
    // added on the BE side, add it here AND to BUILTIN_COMMANDS
    // in the same change so they stay in lockstep.
    //
    // ``/exit`` intentionally absent — it aliases ``/quit`` on the
    // BE and listing both would clutter the menu with the same
    // action under two names.
    const REQUIRED = [
      "/help",
      "/clear",
      "/compact",
      "/ctx",
      "/sessions",
      "/fork",
      "/rename",
      "/model",
      "/login",
      "/logout",
      "/whoami",
      "/mcp",
      "/agents",
      "/skills",
      "/plugin",
      "/plugins",
      "/knowledge",
      "/codeindex",
      "/sync-knowledge",
      "/hooks",
      "/loop",
      "/schedule",
      "/memory",
      "/config",
      "/output-style",
      "/plan",
      "/accept",
      "/bypass",
      "/evals",
      "/bug",
      "/quit",
    ];
    const names = BUILTIN_COMMANDS.map((c) => c.name);
    for (const required of REQUIRED) {
      expect(names).toContain(required);
    }
  });
});

describe("filterSlashCommands", () => {
  // Use a stable mini-pool so the assertions don't churn when
  // someone adds a new built-in.
  const pool: SlashCommand[] = [
    { name: "/help", description: "Show help" },
    { name: "/clear", description: "Clear chat" },
    { name: "/compact", description: "Compact context" },
    { name: "/codeindex", description: "Code index" },
    { name: "/sessions", description: "List sessions" },
  ];

  it("returns the full pool for an empty query", () => {
    // User just typed ``/`` — show everything. Each command's
    // ``slice(1).startsWith("")`` is trivially true.
    expect(filterSlashCommands(pool, "")).toEqual(pool);
  });

  it("prefix-matches on the name AFTER the leading '/'", () => {
    // Query ``co`` should match ``/compact`` and ``/codeindex``
    // but not ``/clear`` (which starts with c, not co).
    const out = filterSlashCommands(pool, "co");
    expect(out.map((c) => c.name)).toEqual(["/compact", "/codeindex"]);
  });

  it("is case-insensitive", () => {
    // The user might shift their hand and type ``/COM`` —
    // still meant ``/compact``. Lowercased on both sides.
    expect(filterSlashCommands(pool, "COM").map((c) => c.name)).toEqual([
      "/compact",
    ]);
    expect(filterSlashCommands(pool, "Help").map((c) => c.name)).toEqual([
      "/help",
    ]);
  });

  it("returns [] when nothing matches", () => {
    // Empty result → the menu collapses, the user sees that
    // their query didn't match anything.
    expect(filterSlashCommands(pool, "zz")).toEqual([]);
  });

  it("does NOT match substring (e.g. 'lear' does not match /clear)", () => {
    // The autocomplete is prefix-based. Substring matching
    // would surface commands for queries that the user wasn't
    // trying to complete (typo'd ``/lear`` → unwanted match).
    expect(filterSlashCommands(pool, "lear")).toEqual([]);
  });

  it("caps at 12 results by default", () => {
    // The dropdown shows ~12 rows. If the user types just
    // ``/`` we don't want to render the whole 18-entry list +
    // every plugin's skill — capping keeps the menu scannable.
    const big: SlashCommand[] = Array.from({ length: 30 }, (_, i) => ({
      name: `/cmd${i.toString().padStart(2, "0")}`,
      description: `desc ${i}`,
    }));
    expect(filterSlashCommands(big, "").length).toBe(12);
  });

  it("custom limit overrides the default cap", () => {
    // The helper takes a limit param — useful for surfaces
    // that want a different cap (e.g. a settings UI listing
    // all commands).
    const big: SlashCommand[] = Array.from({ length: 30 }, (_, i) => ({
      name: `/cmd${i.toString().padStart(2, "0")}`,
      description: `desc ${i}`,
    }));
    expect(filterSlashCommands(big, "", 3).length).toBe(3);
    // limit=0 → empty (defensive; the helper doesn't ban it).
    expect(filterSlashCommands(big, "", 0).length).toBe(0);
  });

  it("preserves source order (no implicit reordering)", () => {
    // The pool order is meaningful — the composer assigns
    // ``active: 0`` to the first match, so reordering the
    // helper's output would silently change which command is
    // highlighted by default.
    const out = filterSlashCommands(pool, "");
    expect(out.map((c) => c.name)).toEqual(pool.map((c) => c.name));
  });

  it("works on a merged pool (built-ins + skills)", () => {
    // The real call site is ``[...BUILTIN_COMMANDS, ...skills]``.
    // Verify the helper has no built-in vs skill awareness —
    // both are just SlashCommand[] entries.
    const skills: SlashCommand[] = [
      { name: "/my-skill", description: "User skill" },
    ];
    const merged = [...pool, ...skills];
    expect(filterSlashCommands(merged, "my-").map((c) => c.name)).toEqual([
      "/my-skill",
    ]);
  });
});
