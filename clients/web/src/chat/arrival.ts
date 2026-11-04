/**
 * A link that opens a window on a page: `?open=<page kind>&<param>=…`.
 *
 * Somebody followed a link — an extension's service builds them, for a
 * document it shared, say — and the link names the page and where on it
 * to go. igni reads neither: the page is any page kind (an extension's
 * usually), and every other query parameter igni does not use itself is
 * handed to that page as its launch params, the same params a
 * notification's target carries.
 *
 * A link is an arrival, not a place: the window reads it once and then
 * takes it out of the address bar (see `App`), so a reload does not
 * keep returning to it.
 */

/** Query parameters igni reads for itself; never a page's. */
const IGNI_PARAMS = new Set([
  "open",
  "dir",
  "ws",
  "demo",
  "debug",
  "expected_cli",
  "actual_cli",
  "backend_source",
]);

export interface Arrival {
  page: string;
  params: Record<string, string>;
}

export const Arrivals = {
  /** The page a window's query string asks for, or `null` for none. */
  fromSearch(search: string): Arrival | null {
    const query = new URLSearchParams(search);
    const page = query.get("open")?.trim() ?? "";
    if (!page) return null;
    const params: Record<string, string> = {};
    for (const [key, value] of query) {
      if (!IGNI_PARAMS.has(key)) params[key] = value;
    }
    return { page, params };
  },

  /** The same query string without the arrival: `open` and its params gone. */
  withoutArrival(search: string): string {
    const query = new URLSearchParams(search);
    if (!query.get("open")) return search;
    const kept = new URLSearchParams();
    for (const [key, value] of query) {
      if (IGNI_PARAMS.has(key) && key !== "open") kept.append(key, value);
    }
    const rest = kept.toString();
    return rest ? `?${rest}` : "";
  },
};
