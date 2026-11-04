// @vitest-environment jsdom
/**
 * A message an extension posted shows the extension's name — live and
 * after a reload, when the history carries only its id and is read
 * before the extensions are listed.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { ChatItemView } from "../components/ChatItems";
import { restoredItem } from "../chat/model";
import { ExtensionNamesContext } from "./extensionNames";

afterEach(() => cleanup());

const POSTED = {
  role: "user",
  content: '<extension-message from="acme.reviewer">\nTwo issues in auth.py.\n</extension-message>',
};

describe("a message an extension posted", () => {
  it("is labelled with the extension's name once it is known", () => {
    const item = restoredItem(POSTED)!;
    expect(item.kind === "user" && item.fromExtension).toBe("acme.reviewer");
    const { rerender } = render(
      <ExtensionNamesContext.Provider value={new Map()}>
        <ChatItemView item={item} />
      </ExtensionNamesContext.Provider>,
    );
    // Not listed yet: the id is all there is.
    expect(screen.getByText("from acme.reviewer")).toBeTruthy();
    rerender(
      <ExtensionNamesContext.Provider value={new Map([["acme.reviewer", "Reviewer"]])}>
        <ChatItemView item={item} />
      </ExtensionNamesContext.Provider>,
    );
    expect(screen.getByText("from Reviewer")).toBeTruthy();
  });
});
