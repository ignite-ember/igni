/**
 * The theme a frame should be told about.
 *
 * The app keeps its theme on `<html data-theme>` rather than in
 * React state — the CSS keys off the attribute, so nothing needed to
 * re-render when it changed. That is fine for the app and not fine
 * for an extension: `ExtensionFrame` posts a `theme` message when
 * its prop changes, and a prop that never changes means a frame that
 * never hears about dark mode.
 *
 * Two sources, because there are two ways it moves: the attribute,
 * set by the IDE bridge and the in-app toggle, and the OS preference
 * when no attribute is set.
 */
import { useEffect, useState } from "react";

/** `"dark"` or `"light"`, as the document currently resolves it. */
export function readDocumentTheme(): string {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === "dark" || explicit === "light") return explicit;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

/** Follow {@link readDocumentTheme}. */
export function useDocumentTheme(): string {
  const [theme, setTheme] = useState(readDocumentTheme);

  useEffect(() => {
    const update = () => setTheme(readDocumentTheme());

    // The attribute, for the IDE bridge and the in-app toggle.
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });

    // The OS preference, which only applies while no attribute is
    // set — `readDocumentTheme` already encodes that precedence, so
    // this just asks it again.
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    media?.addEventListener("change", update);

    // Between the first render and this effect the attribute may
    // already have moved, and a `MutationObserver` only reports what
    // happens after it starts watching.
    update();

    return () => {
      observer.disconnect();
      media?.removeEventListener("change", update);
    };
  }, []);

  return theme;
}
