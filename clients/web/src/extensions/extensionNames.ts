/**
 * Extensions' display names, by id, for whatever paints one.
 *
 * A message an extension posted (`host.chat.post`) carries the
 * extension's *id* — live, and in the history a reload restores, where
 * the id is all the session log keeps. The bubble shows the name, looked
 * up when it renders rather than when the message arrived: on a reload
 * the history is read before the extensions are listed, and a name
 * fixed at arrival would be the id for good.
 */

import { createContext, useContext } from "react";

export const ExtensionNamesContext = createContext<ReadonlyMap<string, string>>(new Map());

/** The display name of the extension with `id`, or the id itself
 *  until (or unless) that extension is listed. */
export function useExtensionName(id: string | undefined): string {
  const names = useContext(ExtensionNamesContext);
  if (!id) return "";
  return names.get(id) ?? id;
}
