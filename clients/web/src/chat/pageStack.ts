/**
 * Where a window is, as a stack.
 *
 * The thirteen browsable destinations — plugins, artifacts, codeindex,
 * mcp, watcher, hooks, schedule, agents, skills, workflows, and the
 * three that came later: loop, help, context. They used to share one
 * slot of `PanelState` and render as a centre-screen modal. One slot
 * means no
 * history: `ArtifactPanel` walks collections into documents and had no
 * way to say where you were or to go back one level, because there was
 * nowhere to put the trail.
 *
 * A stack is that trail. The last entry is what renders; everything
 * below it is a breadcrumb. An empty stack means the chat is showing.
 *
 * Pure, and separate from the component, for the reason
 * `chat/runPhase.ts` gives about the flags it replaced: navigation
 * rules that live inside a 3000-line component get re-implemented
 * slightly differently at each of the call sites that need them.
 *
 * Not a router. The three hosts that load this bundle all enter at
 * `index.html?ws=…` inside a webview, so browser history is a liability
 * rather than a feature, and the state was already a discriminated
 * union — it just had one slot instead of several.
 */

/**
 * The destinations that are pages. Dialogs stay in `PanelState`.
 *
 * A string, not a union of the thirteen this build happens to ship.
 * An extension contributes pages, and a closed union cannot admit one:
 * the set of destinations is decided at install time by what an
 * administrator put on the machine, not at compile time by us.
 *
 * What the union was buying — a typo failing the build — moves to
 * registration, which is where it has to be anyway. `PageRegistry`
 * below is what answers "is this a real destination?", and it answers
 * at the moment the question is actually asked.
 */
export type PageKind = string;

/** The destinations this build ships with. Extensions add to these. */
export const BASE_PAGE_KINDS = [
  "plugins",
  "extensions",
  "artifacts",
  "codeindex",
  "mcp",
  "watcher",
  "hooks",
  "schedule",
  "agents",
  "skills",
  "workflows",
  "loop",
  "help",
  "context",
] as const;

/** One of the fourteen above — still exact, for the app's own code. */
export type BasePageKind = (typeof BASE_PAGE_KINDS)[number];

/**
 * Which destinations exist right now.
 *
 * Holds the base kinds plus whatever the installed extensions
 * contribute, and knows which extension owns each one so that
 * disabling an extension takes its pages with it.
 *
 * A contribution that collides with a base name, or with another
 * extension's, is refused here rather than quietly shadowing it. The
 * contract calls that a validation error at install; this is the
 * runtime half of the same rule, for the case where an install was
 * never validated.
 */
export class PageRegistry {
  private readonly base: ReadonlySet<string>;
  private readonly contributed = new Map<string, string>();

  constructor(base: readonly string[] = BASE_PAGE_KINDS) {
    this.base = new Set(base);
  }

  /**
   * Record a page an extension contributes.
   *
   * @returns the reason it was refused, or `null` when accepted.
   */
  add(kind: string, owner: string): string | null {
    if (!kind) return "a page needs a kind";
    if (this.base.has(kind)) {
      return `'${kind}' is one of igni's own pages, so '${owner}' cannot contribute it`;
    }
    const existing = this.contributed.get(kind);
    if (existing !== undefined && existing !== owner) {
      return `'${kind}' is already contributed by '${existing}'`;
    }
    this.contributed.set(kind, owner);
    return null;
  }

  /** Forget everything one extension contributed. */
  removeOwner(owner: string): void {
    for (const [kind, by] of [...this.contributed]) {
      if (by === owner) this.contributed.delete(kind);
    }
  }

  has(kind: string): boolean {
    return this.base.has(kind) || this.contributed.has(kind);
  }

  /** The extension that contributed this page, if one did. */
  ownerOf(kind: string): string | null {
    return this.contributed.get(kind) ?? null;
  }

  kinds(): string[] {
    return [...this.base, ...this.contributed.keys()];
  }
}

/** What the app uses when nobody passes a registry of their own. */
export const basePages = new PageRegistry();

export interface PageRoute {
  kind: PageKind;
  /** What this level is called in the breadcrumb trail. */
  label: string;
  /**
   * Enough to re-open this level and nothing more — an id, a name, a
   * collection. Never fetched content: the stack is persisted per
   * window, and a cache in `client_state` would go stale silently
   * while looking authoritative.
   */
  params?: Record<string, string>;
}

/** The route that renders, or `null` when the chat is showing. */
export function current(stack: readonly PageRoute[]): PageRoute | null {
  return stack.length ? stack[stack.length - 1] : null;
}

/**
 * Go to a destination — a header link, or a tools-menu entry.
 *
 * Always a fresh trail. Destinations are siblings, not nested: a
 * breadcrumb reading `Plugins / Artifacts` would claim Artifacts lives
 * inside Plugins, and clicking "Plugins" while three levels deep in
 * Plugins should land at the top of Plugins rather than add a fourth
 * level to it.
 *
 * Distinct from [`push`] because the two cannot be told apart by their
 * argument — a plugin's detail page has `kind: "plugins"` exactly like
 * the destination it sits inside, so a single function switching on
 * kind collapses one into the other. (It did, and the test caught it.)
 */
export function openRoot(route: PageRoute): PageRoute[] {
  return [route];
}

/** Drill one level deeper into the destination you are already in. */
export function push(
  stack: readonly PageRoute[],
  route: PageRoute,
): PageRoute[] {
  return [...stack, route];
}

/** Go back one level; at the root this returns to the chat. */
export function pop(stack: readonly PageRoute[]): PageRoute[] {
  return stack.slice(0, -1);
}

/** Jump to a breadcrumb, dropping everything above it. */
export function truncateTo(
  stack: readonly PageRoute[],
  index: number,
): PageRoute[] {
  if (index < 0) return [];
  return stack.slice(0, index + 1);
}

/** Back to the chat. */
export function reset(): PageRoute[] {
  return [];
}

/** Serialise for `client_state`. */
export function serialise(stack: readonly PageRoute[]): string {
  return JSON.stringify(stack);
}

/**
 * Restore a stack, or an empty one from anything unusable.
 *
 * Two reasons this is forgiving rather than strict. The value survives
 * across releases, so a route kind that existed last version may not
 * exist now — and a window that refuses to render because it cannot
 * parse where it used to be is worse than one that opens on the chat.
 *
 * An unknown kind truncates the trail rather than being skipped. A
 * stack is a path: with the middle missing, what is above it has no
 * context to sit in, and a breadcrumb trail with a hole in it says
 * something untrue about how the reader got there. (The docstring
 * here used to say "dropped individually", which the code has never
 * done — it has always broken out of the loop.)
 *
 * `known` decides what counts. The set is no longer fixed at compile
 * time: an extension contributes pages, so a saved stack may name a
 * destination that exists only while that extension is installed and
 * enabled. Passing the live registry is what makes a disabled
 * extension's page restore as "back to the chat" rather than as a
 * route to a surface that is not there.
 */
export function restore(
  raw: string | null | undefined,
  known: { has(kind: string): boolean } = basePages,
): PageRoute[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const out: PageRoute[] = [];
    for (const entry of parsed) {
      if (!entry || typeof entry !== "object") break;
      const { kind, label, params } = entry as Record<string, unknown>;
      if (typeof kind !== "string" || !known.has(kind)) break;
      out.push({
        kind: kind as PageKind,
        label: typeof label === "string" ? label : kind,
        ...(params && typeof params === "object"
          ? { params: params as Record<string, string> }
          : {}),
      });
    }
    return out;
  } catch {
    return [];
  }
}
