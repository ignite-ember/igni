/**
 * Where a window is, as a stack.
 *
 * The browsable destinations — plugins, knowledge, codeindex, mcp,
 * watcher, hooks, schedule, agents, skills, and the three that came
 * later: loop, help, context, workflows. They used to share one slot of
 * `PanelState` and render as a centre-screen modal. One slot means no
 * history: `KnowledgePanel` walks collections into documents and had no
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

/** The destinations that are pages. Dialogs stay in `PanelState`. */
export type PageKind =
  | "plugins"
  | "knowledge"
  | "codeindex"
  | "mcp"
  | "watcher"
  | "hooks"
  | "schedule"
  | "agents"
  | "skills"
  | "loop"
  | "help"
  | "context"
  | "workflows";

const PAGE_KINDS: ReadonlySet<string> = new Set<PageKind>([
  "plugins",
  "knowledge",
  "codeindex",
  "mcp",
  "watcher",
  "hooks",
  "schedule",
  "agents",
  "skills",
  "loop",
  "help",
  "context",
  "workflows",
]);

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
 * breadcrumb reading `Plugins / Knowledge` would claim Knowledge lives
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
export function push(stack: readonly PageRoute[], route: PageRoute): PageRoute[] {
  return [...stack, route];
}

/** Go back one level; at the root this returns to the chat. */
export function pop(stack: readonly PageRoute[]): PageRoute[] {
  return stack.slice(0, -1);
}

/** Jump to a breadcrumb, dropping everything above it. */
export function truncateTo(stack: readonly PageRoute[], index: number): PageRoute[] {
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
 * Unknown kinds are dropped individually, so a stack that is half
 * recognisable restores the half that is.
 */
export function restore(raw: string | null | undefined): PageRoute[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const out: PageRoute[] = [];
    for (const entry of parsed) {
      if (!entry || typeof entry !== "object") break;
      const { kind, label, params } = entry as Record<string, unknown>;
      if (typeof kind !== "string" || !PAGE_KINDS.has(kind)) break;
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
