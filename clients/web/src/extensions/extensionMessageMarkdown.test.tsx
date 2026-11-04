// @vitest-environment jsdom
/**
 * A message an extension posts — `host.chat.post`, or its service's,
 * labelled "from <app>" — is markdown, rendered as the agent's replies
 * are and sanitised the same way: `**bold**` is bold, and raw HTML in
 * the text stays text.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { ChatItemView } from "../components/ChatItems";
import { restoredItem, userItem, type ChatItem } from "../chat/model";
import { ExtensionNamesContext } from "./extensionNames";

afterEach(() => cleanup());

const NAMES = new Map([["acme.claims", "Claims desk"]]);

function renderItem(item: ChatItem) {
  return render(
    <ExtensionNamesContext.Provider value={NAMES}>
      <ChatItemView item={item} />
    </ExtensionNamesContext.Provider>,
  );
}

describe("a message an extension posted", () => {
  it("renders its markdown, under the from label", () => {
    const { container } = renderItem(
      userItem("Claim **C-1042** needs a decision:\n\n- photos\n- invoice", "acme.claims"),
    );
    expect(container.querySelector(".msg-user-from")?.textContent).toBe("from Claims desk");
    expect(container.querySelector("strong")?.textContent).toBe("C-1042");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.textContent).not.toContain("**");
  });

  it("renders its markdown after a reload", () => {
    const item = restoredItem({
      role: "user",
      content: '<extension-message from="acme.claims">\nSee **this**.\n</extension-message>',
    })!;
    const { container } = renderItem(item);
    expect(container.querySelector("strong")?.textContent).toBe("this");
    expect(container.querySelector(".msg-user-from")?.textContent).toBe("from Claims desk");
  });

  it("does not render raw HTML in the text", () => {
    const { container } = renderItem(
      userItem('<img src=x onerror="alert(1)"><script>alert(2)</script> ok', "acme.claims"),
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
  });

  it("leaves a person's own message as it was typed", () => {
    const { container } = renderItem(userItem("keep **these** stars"));
    expect(container.querySelector("strong")).toBeNull();
    expect(container.textContent).toContain("keep **these** stars");
  });
});
