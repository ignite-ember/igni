// @vitest-environment jsdom
/**
 * The plugins page: browse marketplaces, install, open a plugin to see
 * what it bundles, enable/disable/remove it, and manage the marketplaces
 * themselves.
 *
 * The README sanitizer has its own file (`PluginsPanel.readme.test.tsx`);
 * this one pins the flows around it — which RPC each action sends, that
 * the list is re-read after a change, that failures are shown rather than
 * swallowed, and that an Install button cannot be pressed twice while its
 * install is still running.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { PluginsPanel } from "./PluginsPanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const INSTALLED = [
  {
    name: "commit-tools",
    description: "Write better commits",
    version: "1.2.0",
    enabled: true,
    source_root: "acme-market",
    pin: "0123456789abcdef",
    has_skills: true,
    has_hooks: true,
  },
  { name: "quiet", enabled: false },
];

const MARKETS = [
  {
    name: "acme-market",
    url: "https://github.com/acme/market",
    last_fetched: "2026-11-01",
    plugins: [
      {
        name: "commit-tools",
        source: "https://github.com/acme/commit-tools.git",
        description: "Write better commits",
      },
      {
        name: "lint-helper",
        source: "https://github.com/acme/plugins.git [lint]",
        description: "Runs the linter",
        version: "0.3.0",
        branch: "stable",
      },
    ],
  },
  { url: "https://git.example/unnamed", plugins: [{ name: "ssh-only", source: "git@host:x.git" }] },
];

const CONTENTS = {
  name: "lint-helper",
  root_path: "https://github.com/acme/plugins.git [lint]",
  skills: [{ name: "lint" }],
  agents: [{ name: "linter", description: "Fixes lint" }, { name: "silent" }],
  hooks: [
    { event: "PreToolUse", count: 1 },
    { event: "Stop", count: 2 },
  ],
  mcp_servers: [
    { name: "eslint", transport: "stdio", command: "npx eslint-mcp" },
    { name: "remote", transport: "sse", command: "" },
  ],
  tools: [{ name: "lint_file" }],
  readme: "# Lint helper\n\nUse **it**.",
};

type Handler = (args: Record<string, unknown>) => unknown;

function panelWith(handlers: Record<string, Handler> = {}) {
  const calls: { method: string; args: Record<string, unknown> }[] = [];
  const defaults: Record<string, Handler> = {
    get_plugin_details: () => INSTALLED,
    get_marketplaces: () => MARKETS,
    get_plugin_contents: () => ({ ...CONTENTS, name: "commit-tools", root_path: "/plugins/commit-tools" }),
    preview_plugin: () => CONTENTS,
  };
  const rpc = vi.fn(async (method: string, args: Record<string, unknown> = {}) => {
    calls.push({ method, args });
    const h = handlers[method] ?? defaults[method];
    return h ? h(args) : null;
  });
  render(<PluginsPanel client={{ rpc } as never} onClose={() => {}} />);
  const sent = (method: string) => calls.filter((c) => c.method === method);
  return { rpc, calls, sent };
}

describe("the marketplace tab", () => {
  it("opens on the catalog, flattened across marketplaces", async () => {
    panelWith();
    expect(await screen.findByText("lint-helper")).toBeTruthy();
    expect(screen.getByText("ssh-only")).toBeTruthy();
    // An unnamed marketplace is labelled by its URL.
    expect(screen.getByText("https://git.example/unnamed")).toBeTruthy();
    // Installed plugins are marked, not offered again.
    expect(screen.getByText("installed")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Install" })).toHaveLength(2);
    expect(screen.getByPlaceholderText("Search 3 plugins across 2 marketplaces…")).toBeTruthy();
  });

  it("searches names, descriptions and sources", async () => {
    panelWith();
    const search = await screen.findByPlaceholderText(/Search 3 plugins/);
    fireEvent.change(search, { target: { value: "LINTER" } });
    expect(screen.getByText("lint-helper")).toBeTruthy();
    expect(screen.queryByText("ssh-only")).toBeNull();
    fireEvent.change(search, { target: { value: "git@host" } });
    expect(screen.getByText("ssh-only")).toBeTruthy();
    fireEvent.change(search, { target: { value: "zzz" } });
    expect(screen.getByText("No plugins match")).toBeTruthy();
  });

  it("says when no marketplace is configured", async () => {
    panelWith({ get_marketplaces: () => null });
    expect(await screen.findByText("No marketplaces configured")).toBeTruthy();
  });

  it("installs by the plugin's source and re-reads the lists", async () => {
    const { sent } = panelWith();
    await screen.findByText("lint-helper");
    fireEvent.click(screen.getByTitle("Install from https://github.com/acme/plugins.git [lint]"));
    await waitFor(() => expect(sent("get_plugin_details")).toHaveLength(2));
    expect(sent("install_plugin")[0].args).toEqual({
      ref: "https://github.com/acme/plugins.git [lint]",
    });
  });

  it("cannot start a second install of the same plugin while the first runs", async () => {
    let finish: () => void = () => {};
    const { sent } = panelWith({
      install_plugin: () => new Promise<void>((r) => (finish = r)),
    });
    await screen.findByText("lint-helper");
    const button = screen.getByTitle(
      "Install from https://github.com/acme/plugins.git [lint]",
    ) as HTMLButtonElement;
    fireEvent.click(button);
    await waitFor(() => expect(button.disabled).toBe(true));
    fireEvent.click(button);
    expect(sent("install_plugin")).toHaveLength(1);
    finish();
    await waitFor(() => expect(sent("get_plugin_details")).toHaveLength(2));
  });

  it("re-fetches the catalogs on Refresh", async () => {
    const { sent } = panelWith();
    await screen.findByText("lint-helper");
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(sent("refresh_marketplaces")).toHaveLength(1));
    expect(sent("refresh_marketplaces")[0].args).toEqual({ name: null });
  });

  it("shows why a change failed", async () => {
    panelWith({
      install_plugin: () => {
        throw new Error("git clone failed");
      },
    });
    await screen.findByText("lint-helper");
    fireEvent.click(screen.getByTitle(/Install from git@host/));
    expect(await screen.findByText("Error: git clone failed")).toBeTruthy();
  });

  it("shows why the plugin list could not be read", async () => {
    panelWith({
      get_plugin_details: () => {
        throw new Error("backend gone");
      },
      get_marketplaces: () => {
        throw new Error("also gone");
      },
    });
    expect(await screen.findByText("Error: backend gone")).toBeTruthy();
    expect(screen.getByText("No marketplaces configured")).toBeTruthy();
  });
});

describe("the installed tab", () => {
  it("lists installed plugins with what they bundle", async () => {
    panelWith();
    fireEvent.click(await screen.findByRole("button", { name: /^Installed/ }));
    expect(screen.getByText("Write better commits")).toBeTruthy();
    expect(screen.getByText("skills")).toBeTruthy();
    expect(screen.getByText("hooks")).toBeTruthy();
    expect(screen.getByText("@01234567")).toBeTruthy();
    expect(screen.getByText("quiet")).toBeTruthy();
  });

  it("accepts the older answer shape wrapped in an object", async () => {
    panelWith({ get_plugin_details: () => ({ plugins: [{ name: "wrapped" }] }) });
    fireEvent.click(await screen.findByRole("button", { name: /^Installed/ }));
    expect(await screen.findByText("wrapped")).toBeTruthy();
  });

  it("says how to get one when nothing is installed", async () => {
    panelWith({ get_plugin_details: () => [] });
    fireEvent.click(await screen.findByRole("button", { name: /^Installed/ }));
    expect(screen.getByText("No plugins installed yet")).toBeTruthy();
  });

  it("installs from a pasted git ref, by button or Enter, and clears it", async () => {
    const { sent } = panelWith();
    fireEvent.click(await screen.findByRole("button", { name: /^Installed/ }));
    const input = screen.getByPlaceholderText(/Install from URL/) as HTMLInputElement;
    const button = screen.getByRole("button", { name: "Install" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.change(input, { target: { value: "  github.com/acme/x  " } });
    fireEvent.click(button);
    await waitFor(() => expect(input.value).toBe(""));
    expect(sent("install_plugin")[0].args).toEqual({ ref: "github.com/acme/x" });
    fireEvent.change(input, { target: { value: "github.com/acme/y" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(sent("install_plugin")).toHaveLength(2));
  });
});

describe("a plugin's page", () => {
  it("previews a marketplace plugin from its source and branch", async () => {
    const { sent } = panelWith();
    fireEvent.click(await screen.findByText("lint-helper"));
    expect(await screen.findByText("Lint helper")).toBeTruthy();
    expect(sent("preview_plugin")[0].args).toEqual({
      source: "https://github.com/acme/plugins.git [lint]",
      branch: "stable",
      subdir: null,
    });
    // The source splits into a link and a subdir chip.
    const link = screen.getByRole("link", { name: "https://github.com/acme/plugins" });
    expect(link.getAttribute("href")).toBe("https://github.com/acme/plugins.git");
    expect(screen.getByText("lint")).toBeTruthy();
    expect(screen.getByText("stable")).toBeTruthy();
    expect(screen.getByText("Fixes lint")).toBeTruthy();
    expect(screen.getByText("npx eslint-mcp")).toBeTruthy();
    expect(screen.getByText("1 handler")).toBeTruthy();
    expect(screen.getByText("2 handlers")).toBeTruthy();
    expect(screen.getByText("lint_file")).toBeTruthy();
    // The tabs give way to the page trail.
    expect(screen.queryByRole("button", { name: /^Installed/ })).toBeNull();
  });

  it("installs from its page, and says so while it runs", async () => {
    let finish: () => void = () => {};
    const { sent } = panelWith({
      install_plugin: () => new Promise<void>((r) => (finish = r)),
    });
    fireEvent.click(await screen.findByText("lint-helper"));
    await screen.findByText("Lint helper");
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    const busy = (await screen.findByRole("button", { name: "Installing…" })) as HTMLButtonElement;
    expect(busy.disabled).toBe(true);
    expect(sent("install_plugin")).toHaveLength(1);
    finish();
    await waitFor(() => expect(sent("get_plugin_details")).toHaveLength(2));
  });

  it("reads an installed plugin's own copy, and offers disable and remove", async () => {
    // A toggle that takes a moment, as it does on a slow runner.
    const { sent } = panelWith({
      set_plugin_enabled: () => new Promise((done) => setTimeout(() => done(null), 50)),
    });
    fireEvent.click(await screen.findByRole("button", { name: /^Installed/ }));
    fireEvent.click(screen.getByText("commit-tools"));
    expect(await screen.findByText("/plugins/commit-tools")).toBeTruthy();
    expect(sent("get_plugin_contents")[0].args).toEqual({ name: "commit-tools" });
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(sent("set_plugin_enabled")).toHaveLength(1));
    expect(sent("set_plugin_enabled")[0].args).toEqual({ name: "commit-tools", enabled: false });
    // The page is busy until the toggle settles, and a press on a busy
    // button does nothing: on a slow runner (Windows CI) Remove was
    // pressed while still disabled and never sent.
    const remove = screen.getByRole("button", { name: "Remove" }) as HTMLButtonElement;
    await waitFor(() => expect(remove.disabled).toBe(false));
    fireEvent.click(remove);
    await waitFor(() => expect(sent("remove_plugin")).toHaveLength(1));
    // Removed: back on the list.
    expect(await screen.findByRole("button", { name: /^Installed/ })).toBeTruthy();
  });

  it("treats a catalog entry that is installed as the installed plugin", async () => {
    const { sent } = panelWith();
    fireEvent.click(await screen.findByText("commit-tools"));
    expect(await screen.findByRole("button", { name: "Disable" })).toBeTruthy();
    expect(sent("get_plugin_contents")).toHaveLength(1);
    expect(sent("preview_plugin")).toHaveLength(0);
  });

  it("offers Enable for a disabled plugin", async () => {
    panelWith({ get_plugin_contents: () => ({ ...CONTENTS, readme: "", root_path: "" }) });
    fireEvent.click(await screen.findByRole("button", { name: /^Installed/ }));
    fireEvent.click(screen.getByText("quiet"));
    expect(await screen.findByRole("button", { name: "Enable" })).toBeTruthy();
  });

  it("says when a plugin bundles nothing", async () => {
    panelWith({
      preview_plugin: () => ({
        name: "ssh-only",
        root_path: "git@host:x.git",
        skills: [],
        agents: [],
        hooks: [],
        mcp_servers: [],
        tools: [],
        readme: "",
      }),
    });
    fireEvent.click(await screen.findByText("ssh-only"));
    expect(await screen.findByText(/doesn't bundle any skills/)).toBeTruthy();
    // A non-http source is shown, not linked.
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("git@host:x")).toBeTruthy();
  });

  it("shows why its contents could not be read", async () => {
    panelWith({
      preview_plugin: () => {
        throw new Error("repository not found");
      },
    });
    fireEvent.click(await screen.findByText("ssh-only"));
    expect(await screen.findByText("repository not found")).toBeTruthy();
  });
});

describe("managing marketplaces", () => {
  async function openMarkets(handlers: Record<string, Handler> = {}) {
    const panel = panelWith(handlers);
    await screen.findByText("lint-helper");
    fireEvent.click(screen.getByRole("button", { name: "Manage marketplaces" }));
    return panel;
  }

  it("lists the registered marketplaces", async () => {
    await openMarkets();
    expect(screen.getByText("Registered marketplaces (2)")).toBeTruthy();
    expect(screen.getByText("2 plugins · fetched 2026-11-01")).toBeTruthy();
    // One without a name cannot be removed by name.
    expect(
      (screen.getByTitle("Cannot remove (no name)") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("adds a marketplace and clears the field", async () => {
    const { sent } = await openMarkets({ add_marketplace: () => ({ text: "Added acme" }) });
    const input = screen.getByPlaceholderText("github.com/owner/repo or git URL…") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "github.com/acme/more" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(input.value).toBe(""));
    expect(sent("add_marketplace")[0].args).toEqual({ url: "github.com/acme/more" });
  });

  it("shows a failure the backend reports as text, and keeps what was typed", async () => {
    await openMarkets({ add_marketplace: () => ({ text: "git error: not a repository" }) });
    const input = screen.getByPlaceholderText("github.com/owner/repo or git URL…") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Add marketplace" }));
    expect(await screen.findByText("Error: git error: not a repository")).toBeTruthy();
    expect(input.value).toBe("nope");
  });

  it("removes and refreshes by name", async () => {
    const { sent } = await openMarkets();
    fireEvent.click(screen.getByTitle("Unregister acme-market"));
    await waitFor(() => expect(sent("remove_marketplace")).toHaveLength(1));
    expect(sent("remove_marketplace")[0].args).toEqual({ name: "acme-market" });
    fireEvent.click(screen.getByRole("button", { name: "Refresh all" }));
    await waitFor(() => expect(sent("refresh_marketplaces")).toHaveLength(1));
  });

  it("says when none is registered", async () => {
    panelWith({ get_marketplaces: () => [] });
    await screen.findByText("No marketplaces configured");
    fireEvent.click(screen.getByRole("button", { name: "Manage marketplaces" }));
    expect(screen.getByText("No marketplaces registered")).toBeTruthy();
  });
});
