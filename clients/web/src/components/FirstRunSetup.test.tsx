// @vitest-environment jsdom
/**
 * The first-run prompt: what a fresh install sees before it has a model.
 *
 * The welcome screen advertises eight capabilities, none of which can
 * run without a model. These assert the ninth thing — the one that
 * actually works on a first run — is present, wired, and does not offer
 * a second action that dead-ends.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FirstRunSetup } from "./FirstRunSetup";

afterEach(cleanup);

describe("FirstRunSetup", () => {
  it("routes its one action to the login dialog", () => {
    const onLogin = vi.fn();
    render(<FirstRunSetup onLogin={onLogin} />);

    fireEvent.click(screen.getByRole("button", { name: /log in to igni cloud/i }));

    expect(onLogin).toHaveBeenCalledTimes(1);
  });

  it("offers exactly one action", () => {
    // A second "use my own model" button lived here and opened the
    // composer's model menu — which is populated from `models.registry`
    // and so is empty on precisely the installs that see this block.
    const { container } = render(<FirstRunSetup onLogin={() => {}} />);

    expect(container.querySelectorAll(".welcome-setup-actions button")).toHaveLength(1);
  });

  it("names the config file, since bringing your own model means editing it", () => {
    const { container } = render(<FirstRunSetup onLogin={() => {}} />);

    // Asserted literally because it is a path on disk, not a product
    // name: `.ember` is what the backend creates and reads.
    expect(container.textContent).toContain("~/.ember/config.yaml");
    expect(container.textContent).toContain("models.registry");
  });
});
