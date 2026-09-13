/**
 * The slash menu offers what the backend can run.
 *
 * It used to offer a hand-maintained copy of that list, which had
 * drifted both ways: one command advertised that does not exist, and
 * nine real ones nobody could discover from the menu.
 */
import { describe, expect, it } from "vitest";
import {
  BUILTIN_COMMANDS,
  CLIENT_ONLY_COMMANDS,
  mergeCommands,
} from "./Composer";

const be = (...names: string[]) =>
  names.map((name) => ({ name, description: `be: ${name}` }));

describe("mergeCommands", () => {
  it("offers what the backend reports", () => {
    const merged = mergeCommands(be("/commit", "/pr", "/watcher"), []);

    // All three were undiscoverable before: real commands the
    // hardcoded list never mentioned.
    expect(merged.map((c) => c.name)).toEqual(
      expect.arrayContaining(["/commit", "/pr", "/watcher"]),
    );
  });

  it("drops a name the backend does not have", () => {
    const merged = mergeCommands(be("/help"), []);

    expect(merged.map((c) => c.name)).not.toContain("/clear");
  });

  it("keeps the GUI's wording where both have an opinion", () => {
    // The backend's descriptions are written for /help; these are
    // written for this menu.
    const curated = BUILTIN_COMMANDS.find((c) => c.name === "/login")!;
    const merged = mergeCommands(be("/login"), []);

    expect(merged.find((c) => c.name === "/login")!.description).toBe(
      curated.description,
    );
  });

  it("uses the backend's wording for commands the GUI never knew about", () => {
    const merged = mergeCommands(be("/gpu-ai"), []);

    expect(merged.find((c) => c.name === "/gpu-ai")!.description).toBe(
      "be: /gpu-ai",
    );
  });

  it("still offers client-only commands the backend cannot know about", () => {
    // `/workflows` is intercepted in App.tsx and never reaches the
    // backend, so its absence from the RPC is correct.
    const merged = mergeCommands(be("/help"), []);

    for (const name of CLIENT_ONLY_COMMANDS) {
      expect(merged.map((c) => c.name)).toContain(name);
    }
  });

  it("falls back to the hardcoded list before the RPC answers", () => {
    // `null` is "not yet", not "nothing" — an empty menu during
    // startup would read as a broken app.
    expect(mergeCommands(null, []).length).toBe(BUILTIN_COMMANDS.length);
  });

  it("falls back when the backend is too old to answer", () => {
    expect(mergeCommands([], []).length).toBe(BUILTIN_COMMANDS.length);
  });

  it("does not list a skill twice when it arrives by both routes", () => {
    // Skills come back inside get_slash_commands (source: "skill")
    // and also via the older get_skill_definitions path.
    const merged = mergeCommands(be("/help", "/my-skill"), [
      { name: "/my-skill", description: "from the skills RPC" },
    ]);

    expect(merged.filter((c) => c.name === "/my-skill")).toHaveLength(1);
  });

  it("appends skills the backend did not report", () => {
    const merged = mergeCommands(be("/help"), [
      { name: "/solo", description: "skill" },
    ]);

    expect(merged.map((c) => c.name)).toContain("/solo");
  });
});
