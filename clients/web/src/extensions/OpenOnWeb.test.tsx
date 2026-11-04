// @vitest-environment jsdom
/**
 * "Open on the web": an extension's home page — its service's
 * `client_uri` — shown where the extension is, and only when known.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ExtensionsPanel } from "../components/panels/ExtensionsPanel";
import type { IgniClient } from "../protocol/client";
import { ExtensionPage, type ExtensionSummary } from "./ExtensionPage";
import { OpenOnWeb } from "./OpenOnWeb";

afterEach(cleanup);

function summary(over: Partial<ExtensionSummary> = {}): ExtensionSummary {
  return {
    id: "acme.reviewer",
    name: "Reviewer",
    version: "1.0.0",
    state: "READY",
    reason: "",
    origin: "http://127.0.0.1:5555",
    allow: [],
    pages: [{ kind: "acme.reviewer.home", label: "Home", sidebar: true, entry: "index.html" }],
    menu: [],
    indicators: [],
    settings: [],
    values: {},
    ...over,
  };
}

describe("Open on the web", () => {
  it("opens the service in the browser, as igni opens every external link", () => {
    const { getByRole } = render(<OpenOnWeb url="https://reviewer.acme.test" name="Reviewer" />);
    const link = getByRole("link", { name: "Open Reviewer on the web" });
    expect(link.getAttribute("href")).toBe("https://reviewer.acme.test");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("is not shown without a service URL", () => {
    const { container } = render(<OpenOnWeb url={null} name="Reviewer" />);
    expect(container.innerHTML).toBe("");
  });

  it("is above the extension's page when it has a web home", () => {
    const { getByRole, container } = render(
      <ExtensionPage
        extension={summary({ web_url: "https://reviewer.acme.test" })}
        kind="acme.reviewer.home"
        onCall={async () => null}
      />,
    );
    expect(getByRole("link", { name: "Open Reviewer on the web" })).toBeTruthy();
    expect(container.querySelector("iframe")).not.toBeNull();
  });

  it("leaves a page without one exactly as it was", () => {
    const { queryByRole, container } = render(
      <ExtensionPage extension={summary()} kind="acme.reviewer.home" onCall={async () => null} />,
    );
    expect(queryByRole("link")).toBeNull();
    expect(container.firstElementChild?.tagName).toBe("IFRAME");
  });

  it("is on the extension's row in the extensions panel", () => {
    const { getAllByRole } = render(
      <ExtensionsPanel
        client={{} as IgniClient}
        extensions={[summary({ web_url: "https://reviewer.acme.test" }), summary({ id: "acme.dev", name: "Dev" })]}
        onChanged={() => {}}
        onClose={() => {}}
      />,
    );
    const links = getAllByRole("link");
    expect(links.map((l) => l.getAttribute("aria-label"))).toEqual(["Open Reviewer on the web"]);
  });
});
