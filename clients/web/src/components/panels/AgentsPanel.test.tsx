// @vitest-environment jsdom
/**
 * An agent made for one task is kept for the team or for you.
 *
 * "Keep" used to be the only way to keep one, and it always wrote to
 * `.igni/agents/` — committed, shared with everybody on the project.
 * "Keep for me" writes to `.igni/agents.local/`, which `.igni/.gitignore`
 * keeps out of git.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AgentsPanel } from "./AgentsPanel";

afterEach(cleanup);

function panel() {
  const rpc = vi.fn(async (method: string) =>
    method === "get_agent_details"
      ? [{ name: "migrator", description: "d", is_ephemeral: true }]
      : { ok: true },
  );
  render(<AgentsPanel client={{ rpc } as never} onClose={() => {}} />);
  return rpc;
}

describe("keeping an ephemeral agent", () => {
  it("keeps it for the project", async () => {
    const rpc = panel();
    fireEvent.click(await screen.findByRole("button", { name: "Keep" }));
    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith("promote_ephemeral_agent", {
        name: "migrator",
        target: "project",
      }),
    );
  });

  it("keeps it for you only", async () => {
    const rpc = panel();
    fireEvent.click(await screen.findByRole("button", { name: "Keep for me" }));
    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith("promote_ephemeral_agent", {
        name: "migrator",
        target: "personal",
      }),
    );
  });
});
