// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ToolCardView } from "./ChatItems";

afterEach(() => cleanup());

describe("ToolCardView", () => {
  it("marks a call that never ran", () => {
    const { container } = render(
      <ToolCardView
        name="Bash"
        args="git push --force origin main"
        status="rejected"
        result="Not run: the user rejected this call when asked to approve it."
      />,
    );
    expect(screen.getByText("Not run")).toBeTruthy();
    expect(container.querySelector(".tool-status.rejected")).toBeTruthy();
  });

  it("shows no tag on a call that ran", () => {
    render(<ToolCardView name="Bash" args="ls" status="done" result="README.md" />);
    expect(screen.queryByText("Not run")).toBeNull();
  });
});
