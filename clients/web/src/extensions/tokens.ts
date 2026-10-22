/**
 * The design tokens an extension may rely on, and how they travel.
 *
 * An extension's page is a different origin, so it cannot read this
 * app's stylesheet — and the CSP on its own origin would block
 * fetching one. So the values are **pushed**, over the bridge, at
 * handshake and again whenever they change.
 *
 * Pushed rather than copied into the package on purpose. A static
 * copy is a second description of the theme, and a second description
 * drifts: `theme.css` defines these across forty-five blocks — a
 * ``:root``, a light override, and a ``prefers-color-scheme`` block
 * per feature. Anything duplicating that is wrong within a release.
 * This reads the live computed values instead, so an extension
 * follows dark mode, light mode, and whatever a group's policy pack
 * has branded, without knowing any of those exist.
 *
 * **This list is the floor.** A token named here is one an extension
 * may build against, and removing or repurposing one is a breaking
 * change under the compatibility policy. A token *not* named here is
 * ours to change freely — which is why the list is curated rather
 * than "every custom property in the stylesheet". Layout internals
 * like ``--rail-width`` and ``--crumb-row-h`` are deliberately absent:
 * they describe this app's chrome, and an extension that positioned
 * itself against them would break the first time we moved something.
 */

/**
 * Published tokens, by what they are for.
 *
 * Grouped because the groups are how an author reads them: pick a
 * surface, pick the text that goes on it, pick a tone for the
 * meaning. Names match the app's own so a reader moving between the
 * two is not translating.
 */
export const PUBLISHED_TOKENS = {
  /** Surfaces, back to front. */
  surface: ["--bg", "--bg-inset", "--bg-surface", "--bg-raised"],
  /** Text, in descending prominence. */
  text: ["--fg", "--fg-muted", "--fg-faint"],
  /** Lines between things. */
  line: ["--border", "--border-soft"],
  /** Meaning. ``*-text`` variants are the contrast-safe ones for type. */
  tone: [
    "--success",
    "--warning",
    "--danger",
    "--danger-text",
    "--accent-blue",
    "--accent-blue-text",
    "--accent-green-text",
    "--accent-orange-text",
    "--accent-red-text",
  ],
  /** The product's own colour. Use sparingly; it means igni. */
  brand: [
    "--ember-orange",
    "--ember-orange-hover",
    "--ember-red",
    "--ember-gradient",
  ],
  /** Shape and type. */
  form: [
    "--radius-sm",
    "--radius",
    "--radius-lg",
    "--font-ui",
    "--font-mono",
    "--shadow-pop",
  ],
  /**
   * The spacing scale. Published so an extension's layout sits on the
   * same rhythm as the app around it rather than next to it.
   *
   * These steps are the ones igni already used — they were written as
   * literals in three hundred and seventy-five places, which is why
   * adopting them changed nothing on screen.
   */
  space: [
    "--space-1",
    "--space-2",
    "--space-3",
    "--space-4",
    "--space-5",
    "--space-6",
    "--space-7",
    "--space-8",
    "--space-10",
    "--space-12",
    "--space-16",
  ],
} as const;

/** Every published token name, flat. */
export const TOKEN_NAMES: readonly string[] =
  Object.values(PUBLISHED_TOKENS).flat();

/** Token name → current value. */
export type ThemeTokens = Record<string, string>;

/**
 * Read the published tokens as they are right now.
 *
 * Computed values, not declared ones: a token defined in a media
 * query or overridden by a `data-theme` attribute resolves here to
 * what the user is actually looking at, which is the only answer
 * worth sending.
 */
export function readThemeTokens(
  root: Element = document.documentElement,
): ThemeTokens {
  const computed = getComputedStyle(root);
  const out: ThemeTokens = {};
  for (const name of TOKEN_NAMES) {
    const value = computed.getPropertyValue(name).trim();
    // A token the host has not defined is omitted rather than sent
    // empty: the guest's stylesheet carries a fallback for each, and
    // an empty string would override it with nothing.
    if (value) out[name] = value;
  }
  return out;
}

/**
 * Apply tokens inside the extension's own document.
 *
 * Called by the guest bridge. Separate from reading them so the two
 * halves can be tested without a frame between them.
 */
export function applyThemeTokens(tokens: ThemeTokens, root: HTMLElement): void {
  for (const [name, value] of Object.entries(tokens)) {
    // Only published names, even though the host is trusted: a typo
    // on our side should not silently define something in theirs.
    if (TOKEN_NAMES.includes(name)) root.style.setProperty(name, value);
  }
}
