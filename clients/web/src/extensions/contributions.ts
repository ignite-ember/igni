/**
 * Where an extension's surfaces go, and what happens when it leaves.
 *
 * Three things in the app were closed lists in module scope: the page
 * kinds, the sidebar links and the tools menu. A module constant is
 * not a contribution point — nothing outside the file can reach it,
 * and the only way to add an entry was to edit the array, which is
 * precisely what an extension cannot do.
 *
 * So the lists become "what this build ships" plus "what is installed
 * now", joined here. The base entries keep their order and come
 * first: an extension cannot push igni's own navigation down the
 * list, and a reader who learned where Plugins sits does not find it
 * somewhere else after an install.
 *
 * Removal is by owner rather than by entry. An extension that is
 * disabled, revoked or upgraded takes every surface it contributed
 * with it in one call, because the alternative is remembering each
 * one — and the thing nobody remembers is the entry that outlives the
 * extension behind it and opens onto nothing.
 */

import { PageRegistry, type PageKind } from "../chat/pageStack";

/** A link in the left sidebar. */
export interface SidebarLink {
  kind: PageKind;
  label: string;
  /** The extension that contributed it, or `null` for igni's own. */
  owner?: string | null;
}

/** An entry in the header tools menu. */
export interface ToolsEntry {
  label: string;
  /** What running it does. A slash command the host already knows. */
  command: string;
  desc: string;
  owner?: string | null;
}

/** One extension's UI contributions, as its manifest declares them. */
export interface UiContribution {
  /** Pages it adds, by kind. */
  pages?: { kind: string; label: string; sidebar?: boolean }[];
  /** Tools-menu entries it adds. */
  tools?: { label: string; command: string; desc: string }[];
}

/** What went wrong with one contributed entry, and whose it was. */
export interface ContributionRefusal {
  owner: string;
  reason: string;
}

/**
 * The app's navigation, with the installed extensions folded in.
 *
 * Construct once with the base lists; call {@link install} and
 * {@link uninstall} as extensions come and go; read {@link sidebar}
 * and {@link tools} when rendering.
 */
export class Contributions {
  readonly pages: PageRegistry;
  private readonly baseSidebar: readonly SidebarLink[];
  private readonly baseTools: readonly ToolsEntry[];
  private readonly extraSidebar = new Map<string, SidebarLink[]>();
  private readonly extraTools = new Map<string, ToolsEntry[]>();

  constructor(
    baseSidebar: readonly SidebarLink[],
    baseTools: readonly ToolsEntry[],
    pages: PageRegistry = new PageRegistry(),
  ) {
    this.baseSidebar = baseSidebar;
    this.baseTools = baseTools;
    this.pages = pages;
  }

  /**
   * Fold one extension's contributions in.
   *
   * Refusals are returned rather than thrown, and the entries that
   * were fine are still installed. One bad page in a manifest should
   * cost that page, not the extension — and the caller needs the list
   * to show the administrator what to fix. Anything refused here was
   * already refused at install; this is the runtime half of the same
   * rule, for an install that was never validated.
   */
  install(owner: string, contribution: UiContribution): ContributionRefusal[] {
    const refusals: ContributionRefusal[] = [];
    const sidebar: SidebarLink[] = [];
    const tools: ToolsEntry[] = [];

    for (const page of contribution.pages ?? []) {
      const refused = this.pages.add(page.kind, owner);
      if (refused !== null) {
        refusals.push({ owner, reason: refused });
        continue;
      }
      if (page.sidebar)
        sidebar.push({ kind: page.kind, label: page.label, owner });
    }

    for (const entry of contribution.tools ?? []) {
      const clash = this.toolsLabelOwner(entry.label);
      if (clash !== null) {
        refusals.push({
          owner,
          reason: `'${entry.label}' is already in the tools menu, from '${clash}'`,
        });
        continue;
      }
      tools.push({ ...entry, owner });
    }

    if (sidebar.length) this.extraSidebar.set(owner, sidebar);
    if (tools.length) this.extraTools.set(owner, tools);
    return refusals;
  }

  /** Take back everything one extension contributed. */
  uninstall(owner: string): void {
    this.pages.removeOwner(owner);
    this.extraSidebar.delete(owner);
    this.extraTools.delete(owner);
  }

  /** The sidebar, igni's own links first. */
  sidebar(): SidebarLink[] {
    return [...this.baseSidebar, ...flatten(this.extraSidebar)];
  }

  /** The tools menu, igni's own entries first. */
  tools(): ToolsEntry[] {
    return [...this.baseTools, ...flatten(this.extraTools)];
  }

  /**
   * Who owns the tools entry with this label — `"igni"` for a base
   * one — or `null` when the label is free.
   */
  private toolsLabelOwner(label: string): string | null {
    if (this.baseTools.some((e) => e.label === label)) return "igni";
    for (const [owner, entries] of this.extraTools) {
      if (entries.some((e) => e.label === label)) return owner;
    }
    return null;
  }
}

/** Entries in insertion order, which is install order. */
function flatten<T>(byOwner: Map<string, T[]>): T[] {
  const out: T[] = [];
  for (const entries of byOwner.values()) out.push(...entries);
  return out;
}
