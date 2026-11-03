// @vitest-environment jsdom
/**
 * The MCP servers page: which servers are configured, whether each is
 * connected, what it offers, and which of its tools the agent may see.
 *
 * Pinned: the page connects configured servers before listing them, a
 * policy-blocked server cannot be connected from here, hiding a tool
 * reacts at once and is undone by a re-read when the backend refuses,
 * and a failed connect leaves the page usable rather than throwing.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { McpPanel } from "./McpPanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const GITHUB = {
  name: "github",
  connected: true,
  transport: "stdio",
  tool_names: ["github__create_issue", "search"],
  tool_descriptions: { github__create_issue: "Open an issue in a repository." },
  tools_disabled: ["search"],
  resources: [
    { uri: "repo://acme/web", name: "acme/web", description: "The web repo", mime_type: "text/plain" },
    { uri: "repo://acme/bare", name: "", description: "", mime_type: "" },
  ],
  prompts: [
    { name: "triage", description: "Triage an issue", arguments: ["issue", "label"] },
    { name: "plain", description: "", arguments: [] },
  ],
};

const DOWN = { name: "jira", connected: false, transport: "sse", error: "401 Unauthorized" };
const BLOCKED = { name: "pastebin", connected: false, transport: "http", policy_blocked: true };
const BARE = { connected: false };

interface Opts {
  details?: unknown;
  setTool?: () => Promise<unknown>;
  toggle?: () => Promise<unknown>;
}

function panelWith({ details = [GITHUB], setTool, toggle }: Opts = {}) {
  const methods: string[] = [];
  const rpc = vi.fn(async (method: string, args?: unknown) => {
    methods.push(method);
    if (method === "get_mcp_server_details") {
      if (details instanceof Error) throw details;
      return details;
    }
    if (method === "set_mcp_tool_enabled" && setTool) return setTool();
    void args;
    return null;
  });
  const mcpToggle = vi.fn(toggle ?? (async () => ({ type: "info" })));
  const onAddServer = vi.fn();
  render(
    <McpPanel
      client={{ rpc, mcpToggle } as never}
      onClose={() => {}}
      onAddServer={onAddServer}
    />,
  );
  return { rpc, mcpToggle, onAddServer, methods };
}

describe("the server list", () => {
  it("connects configured servers before asking what they offer", async () => {
    const { methods } = panelWith();
    await screen.findByText(/github/);
    expect(methods.slice(0, 2)).toEqual(["ensure_mcp", "get_mcp_server_details"]);
  });

  it("says when no server is configured, including after a failed read", async () => {
    panelWith({ details: [] });
    expect(await screen.findByText("No MCP servers configured.")).toBeTruthy();
    cleanup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    panelWith({ details: new Error("backend gone") });
    expect(await screen.findByText("No MCP servers configured.")).toBeTruthy();
  });

  it("summarises a connected server and explains one that is not", async () => {
    panelWith({ details: [GITHUB, DOWN, BLOCKED, BARE] });
    expect(
      await screen.findByText("2 tools · 2 resources · 2 prompts — click to expand"),
    ).toBeTruthy();
    expect(screen.getByText("401 Unauthorized")).toBeTruthy();
    expect(screen.getByText("blocked by policy")).toBeTruthy();
    expect(screen.getByText("disconnected")).toBeTruthy();
  });

  it("does not let a policy-blocked server be connected", async () => {
    panelWith({ details: [BLOCKED] });
    await screen.findByText("blocked by policy");
    expect((screen.getByRole("button", { name: "Connect" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("hands the composer a setup prompt rather than a form", async () => {
    const { onAddServer } = panelWith();
    fireEvent.click(screen.getByRole("button", { name: "+ Add server" }));
    const [seed] = onAddServer.mock.calls[0];
    expect(seed).toMatch(/Help me set up a new MCP server/);
    expect(seed).toMatch(/mcpServers/);
  });
});

describe("connecting and disconnecting", () => {
  it("disconnects a connected server and re-reads the list", async () => {
    const { mcpToggle, methods } = panelWith();
    await screen.findByText(/click to expand/);
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(mcpToggle).toHaveBeenCalledWith("github", false);
    await waitFor(() =>
      expect(methods.filter((m) => m === "get_mcp_server_details")).toHaveLength(2),
    );
  });

  it("shows the button busy while the toggle is in flight", async () => {
    let finish: () => void = () => {};
    panelWith({
      details: [DOWN],
      toggle: () => new Promise((r) => (finish = () => r({ type: "info" }))),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
    expect(screen.getByRole("button", { name: "…" })).toBeTruthy();
    finish();
    expect(await screen.findByRole("button", { name: "Connect" })).toBeTruthy();
  });

  it("stays usable when a connect fails", async () => {
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    const { methods } = panelWith({
      details: [DOWN],
      toggle: async () => {
        throw new Error("request timed out");
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
    await waitFor(() =>
      expect(methods.filter((m) => m === "get_mcp_server_details")).toHaveLength(2),
    );
    expect(screen.getByRole("button", { name: "Connect" })).toBeTruthy();
    expect(err).toHaveBeenCalled();
  });
});

describe("what a server offers", () => {
  it("expands into tools, resources and prompts", async () => {
    panelWith();
    fireEvent.click(await screen.findByText(/click to expand/));
    // The server prefix Agno adds is split off the tool's name.
    expect(screen.getByText("create_issue")).toBeTruthy();
    expect(screen.getByText("Open an issue in a repository.")).toBeTruthy();
    expect(screen.getByText("off")).toBeTruthy();
    expect(screen.getByText("acme/web")).toBeTruthy();
    expect(screen.getByText("text/plain · repo://acme/web")).toBeTruthy();
    // A resource with no name is named by its uri.
    expect(screen.getAllByText("repo://acme/bare").length).toBeGreaterThan(0);
    expect(screen.getByText("(issue, label)")).toBeTruthy();
    expect(screen.getByText("Triage an issue")).toBeTruthy();
  });

  it("does not expand a server with nothing to show", async () => {
    panelWith({ details: [DOWN] });
    fireEvent.click(await screen.findByText("401 Unauthorized"));
    expect(document.querySelector(".mcp-server-body")).toBeNull();
  });

  it("hides a tool from the agent at once and tells the backend", async () => {
    const { rpc } = panelWith();
    fireEvent.click(await screen.findByText(/click to expand/));
    const hide = screen.getByTitle("Hide from agent");
    fireEvent.click(hide);
    expect(screen.getAllByText("off")).toHaveLength(2);
    expect(rpc).toHaveBeenCalledWith("set_mcp_tool_enabled", {
      server: "github",
      tool: "github__create_issue",
      enabled: false,
    });
  });

  it("turns a hidden tool back on", async () => {
    const { rpc } = panelWith();
    fireEvent.click(await screen.findByText(/click to expand/));
    fireEvent.click(screen.getByTitle("Enable for agent"));
    expect(screen.queryByText("off")).toBeNull();
    expect(rpc).toHaveBeenCalledWith("set_mcp_tool_enabled", {
      server: "github",
      tool: "search",
      enabled: true,
    });
  });

  it("re-reads the truth when the backend refuses a tool change", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const { methods } = panelWith({
      setTool: async () => {
        throw new Error("refused");
      },
    });
    fireEvent.click(await screen.findByText(/click to expand/));
    fireEvent.click(screen.getByTitle("Enable for agent"));
    await waitFor(() =>
      expect(methods.filter((m) => m === "get_mcp_server_details")).toHaveLength(2),
    );
  });

  it("offers Show more only for a description that overflows", async () => {
    // jsdom lays nothing out, so heights are given: taller than shown.
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(80);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(30);
    panelWith();
    fireEvent.click(await screen.findByText(/click to expand/));
    const [more] = screen.getAllByRole("button", { name: "Show more…" });
    fireEvent.click(more);
    expect(screen.getByRole("button", { name: "Show less" })).toBeTruthy();
  });
});
