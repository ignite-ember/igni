/**
 * Applying a group's brand overrides.
 *
 * A group configured in the portal can override a handful of brand
 * tokens; the values arrive on `status_update` and land on the CSS
 * custom properties `theme.css` already defines. Because those
 * properties are defined for both palettes, light and dark keep working
 * untouched — the group brands the product, it does not choose light or
 * dark for anybody.
 *
 * **Two rules, both load-bearing.**
 *
 * *Never build CSS text.* Every value goes through
 * `style.setProperty(prop, value)`. Concatenating an admin-supplied
 * string into a stylesheet — `` `:root{--accent:${v}}` `` — lets a value
 * close the declaration and open its own rules: `#fff;} body {
 * display:none } .x {` is a defacement, and worse is available. With
 * `setProperty` a malformed value is dropped by the CSSOM instead of
 * being parsed as syntax.
 *
 * *Validate here, not only on the server.* The server already rejects
 * everything below, so this is the second check — and it is the one
 * that matters, because a client can be pointed at a server somebody
 * else runs. The codebase reasons the same way about entry names in
 * `schema/portal/groups.py`.
 *
 * The brand mark is deliberately not handled here: it is rendered as
 * `<img src={mark}>` by the header. An SVG *document* can carry script;
 * the same bytes in an `<img>` cannot run. Inlining it as markup would
 * hand an org admin script execution in every one of their users'
 * clients.
 */

import type { GroupTheme } from "../protocol/messages";

/** `#rgb`, `#rrggbb`, `#rrggbbaa` — the grammar the server enforces.
 *
 *  Hex only, matching the server: `rgb()` takes nested expressions and
 *  named colours would need enumerating to be checked at all. A colour
 *  picker emits hex. */
const HEX_COLOUR = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

/**
 * Which theme keys may touch which custom properties.
 *
 * An allowlist rather than a prefix rule (`--${key}`): the key arrives
 * over the wire, and deriving a property name from it would let the
 * server write *any* custom property, including ones later code comes
 * to trust for something other than colour.
 *
 * `accent` writes two. `theme.css` reads `--ember-orange` in 47 places
 * and bare `--accent` in 17 more — and never defines `--accent`, so
 * those 17 fall back to igni's orange no matter what the group asked
 * for. Both are set, which is the difference between branding most of
 * the product and branding most of it except the bits that happened to
 * use the other name.
 *
 * There is deliberately no hover key. Hover states are `color-mix` of
 * `--accent`, so they follow it — a separate control would be one that
 * does nothing.
 *
 * Not set, and worth knowing: `--accent-orange-text` (43 uses) is a
 * *defined* token with a value tuned per palette for contrast — F138's
 * work. Pointing it at a raw brand colour would silently undo that, so
 * brand-tinted label text stays igni's until somebody derives a
 * contrast-checked variant from the accent.
 */
const COLOUR_TOKENS: ReadonlyArray<[keyof GroupTheme, readonly string[]]> = [
  ["accent", ["--ember-orange", "--accent"]],
  ["danger", ["--danger"]],
  ["success", ["--success"]],
];

/** Properties this module owns, so a theme going away can clear exactly
 *  what it set and nothing else. */
const OWNED_PROPERTIES = [
  ...COLOUR_TOKENS.flatMap(([, props]) => props),
  "--ember-gradient",
];

export interface AppliedTheme {
  /** Custom properties actually written, for tests and debugging. */
  applied: Record<string, string>;
  /** Keys that were present but rejected. */
  rejected: string[];
}

/**
 * Apply `theme` to `root`, returning what was set and what was refused.
 *
 * Idempotent and reversible: every call first clears the properties
 * this module owns, so removing a group's branding — or moving a user
 * to a group without any — restores the shipped palette rather than
 * leaving the last theme stuck until reload.
 */
export function applyTheme(
  theme: GroupTheme | null | undefined,
  root: HTMLElement,
): AppliedTheme {
  for (const prop of OWNED_PROPERTIES) root.style.removeProperty(prop);

  const applied: Record<string, string> = {};
  const rejected: string[] = [];
  if (!theme) return { applied, rejected };

  for (const [key, props] of COLOUR_TOKENS) {
    const value = theme[key];
    if (value == null || value === "") continue;
    if (typeof value !== "string" || !HEX_COLOUR.test(value)) {
      rejected.push(key);
      continue;
    }
    for (const prop of props) {
      root.style.setProperty(prop, value);
      applied[prop] = value;
    }
  }

  // The gradient is composed, not configured. It appears across the
  // header and primary buttons, and leaving it at igni's orange while
  // the accent moved would read as a bug rather than as branding.
  // Built from values already validated above.
  const from = applied["--ember-orange"];
  const to = applied["--danger"] ?? from;
  if (from) {
    const gradient = `linear-gradient(135deg, ${from}, ${to})`;
    root.style.setProperty("--ember-gradient", gradient);
    applied["--ember-gradient"] = gradient;
  }

  return { applied, rejected };
}

/** The wordmark to show — the group's, or igni's. */
export function brandName(theme: GroupTheme | null | undefined): string {
  const name = theme?.brand_name?.trim();
  return name || "igni";
}

/**
 * The brand mark's `data:` URI, or null for the shipped mark.
 *
 * Re-checked against the same shape the server accepts. A remote URL
 * would be refused by the desktop client's CSP anyway (`img-src 'self'
 * data: blob:`), so anything that is not a `data:` image is a
 * misconfiguration worth ignoring rather than rendering as a broken
 * image.
 */
export function brandMark(theme: GroupTheme | null | undefined): string | null {
  const mark = theme?.brand_mark;
  if (!mark || typeof mark !== "string") return null;
  return /^data:image\/(?:png|jpeg|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$/.test(mark)
    ? mark
    : null;
}
