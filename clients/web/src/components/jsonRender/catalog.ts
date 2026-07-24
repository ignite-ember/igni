/**
 * The json-render catalog — single source of truth for the 41
 * components we render inside a ``<JsonRenderView>``.
 *
 * This file defines the SHAPE (Zod schemas for each component's props
 * + a short description + an example). The React implementations that
 * consume this catalog live in ``JsonRenderView.tsx`` and are wired
 * via ``defineRegistry(catalog, { components: {...} })`` — same
 * catalog, one place to change.
 *
 * The visualizer sub-agent's system prompt is AUTO-GENERATED from
 * this catalog by ``scripts/gen-visualizer-prompt.mjs`` — never
 * hand-edit the schema docs in ``bundled_agents/visualizer.md``.
 */

import { schema as reactSchema } from "@json-render/react/schema";
import { z } from "zod";

// ── Common types used across components ─────────────────────────────

const gapZ = z.enum(["sm", "md", "lg"]).optional();
const toneZ = z.enum(["neutral", "info", "success", "warning", "danger"]).optional();
const alertToneZ = z.enum(["info", "success", "warning", "danger"]);
const alignZ = z.enum(["left", "right", "center"]).optional();
const variantZ = z.enum(["primary", "secondary", "ghost", "danger"]).optional();
const inputTypeZ = z.enum(["text", "email", "password", "number", "url"]).optional();

const seriesPointZ = z.object({
  x: z.union([z.string(), z.number()]),
  y: z.number(),
});
const seriesDataZ = z.array(seriesPointZ);

const chartPropsZ = {
  data: seriesDataZ.describe("Points to plot; x is the category/time label, y is the value"),
  xLabel: z.string().optional(),
  yLabel: z.string().optional(),
  yPrefix: z.string().optional().describe('Prepended to y-axis tick labels, e.g. "$"'),
  ySuffix: z.string().optional().describe('Appended to y-axis tick labels, e.g. "ms"'),
};

const labelValueZ = z.object({ label: z.string(), value: z.string() });

// ── Catalog ─────────────────────────────────────────────────────────

export const catalog = reactSchema.createCatalog({
  actions: {},
  components: {
    // ── Layout & structure ─────────────────────────────────────────
    Stack: {
      description: "Vertical column of children.",
      slots: ["default"],
      props: z.object({ gap: gapZ }),
      example: { gap: "md" },
    },
    Grid: {
      description: "Responsive grid — split children into N equal columns.",
      slots: ["default"],
      props: z.object({
        columns: z.number().int().min(1).max(12).describe("Column count (1–12)"),
        gap: gapZ,
      }),
      example: { columns: 3, gap: "md" },
    },
    Card: {
      description: "Bordered container with optional title and subtitle.",
      slots: ["default"],
      props: z.object({ title: z.string().optional(), subtitle: z.string().optional() }),
      example: { title: "AAPL — Monthly Close", subtitle: "2023" },
    },
    Carousel: {
      description: "One child visible at a time; prev/next controls + dots.",
      slots: ["default"],
      props: z.object({ activeIndex: z.number().int().nonnegative().optional() }),
      example: { activeIndex: 0 },
    },
    Accordion: {
      description: "Vertical list of collapsible items (each item is title + plain-text body).",
      props: z.object({
        items: z.array(z.object({ title: z.string(), body: z.string().optional() })),
        allowMultiple: z.boolean().optional(),
      }),
      example: { items: [{ title: "Overview", body: "..." }], allowMultiple: false },
    },
    Tabs: {
      description: "Horizontal tab strip. children order matches tabs order (one panel per tab).",
      slots: ["default"],
      props: z.object({
        tabs: z.array(z.object({ label: z.string(), key: z.string().optional() })),
        activeIndex: z.number().int().nonnegative().optional(),
      }),
      example: { tabs: [{ label: "Overview" }, { label: "Details" }], activeIndex: 0 },
    },
    Dialog: {
      description:
        "Inline attention panel with a title bar (NOT a modal overlay — the spec lives inside a chat item).",
      slots: ["default"],
      props: z.object({ title: z.string().optional(), open: z.boolean().optional() }),
      example: { title: "Confirm?", open: true },
    },
    Drawer: {
      description: "Inline drawer variant of Dialog. Same layout, different visual weight.",
      slots: ["default"],
      props: z.object({ title: z.string().optional(), open: z.boolean().optional() }),
      example: { title: "Details", open: true },
    },

    // ── Typography ────────────────────────────────────────────────
    Heading: {
      description: "Section heading (h1–h4).",
      props: z.object({
        text: z.string(),
        level: z.union([z.literal(1), z.literal(2), z.literal(3), z.literal(4)]).optional(),
      }),
      example: { text: "Results", level: 2 },
    },
    Text: {
      description: "Paragraph / inline text with optional muted or monospace styling.",
      props: z.object({
        text: z.string(),
        muted: z.boolean().optional(),
        mono: z.boolean().optional(),
      }),
      example: { text: "A short description.", muted: false },
    },

    // ── Data display ──────────────────────────────────────────────
    Table: {
      description:
        "Small tabular data. columns[].key must match keys in each rows[] object; align is per-column.",
      props: z.object({
        columns: z.array(
          z.object({ key: z.string(), label: z.string(), align: alignZ }),
        ),
        rows: z.array(z.record(z.string(), z.union([z.string(), z.number(), z.boolean()]))),
      }),
      example: {
        columns: [
          { key: "name", label: "Name" },
          { key: "score", label: "Score", align: "right" },
        ],
        rows: [
          { name: "A", score: 42 },
          { name: "B", score: 91 },
        ],
      },
    },
    LineGraph: {
      description: "Line chart. Ideal for time series or ordered categories.",
      props: z.object(chartPropsZ),
      example: {
        data: [
          { x: "Jan", y: 100 },
          { x: "Feb", y: 118 },
          { x: "Mar", y: 132 },
        ],
        yPrefix: "$",
        xLabel: "Month",
      },
    },
    BarGraph: {
      description: "Bar chart. Ideal for comparing values across discrete categories.",
      props: z.object(chartPropsZ),
      example: {
        data: [
          { x: "Q1", y: 25 },
          { x: "Q2", y: 41 },
          { x: "Q3", y: 33 },
        ],
      },
    },
    Candlestick: {
      description:
        "OHLC candlestick chart. Ideal for stock/asset price series where you have open, high, low, close per period. Volume bars are drawn under the price panel when present. Rising candles use `upColor` (default green), falling candles use `downColor` (default red).",
      props: z.object({
        data: z
          .array(
            z.object({
              x: z.union([z.string(), z.number()]).describe("Period label (date, index, or category)"),
              o: z.number().describe("Open price"),
              h: z.number().describe("High price"),
              l: z.number().describe("Low price"),
              c: z.number().describe("Close price"),
              v: z.number().optional().describe("Volume, optional"),
            }),
          )
          .describe("Ordered OHLC(V) rows, one per period"),
        xLabel: z.string().optional(),
        yLabel: z.string().optional(),
        yPrefix: z.string().optional().describe('Prepended to y-axis tick labels, e.g. "$"'),
        ySuffix: z.string().optional(),
        upColor: z.string().optional().describe("Color for candles where close >= open"),
        downColor: z.string().optional().describe("Color for candles where close < open"),
      }),
      example: {
        data: [
          { x: "Mon", o: 100, h: 108, l: 98, c: 105, v: 1200 },
          { x: "Tue", o: 105, h: 110, l: 102, c: 103, v: 950 },
          { x: "Wed", o: 103, h: 112, l: 103, c: 111, v: 1400 },
        ],
        yPrefix: "$",
        xLabel: "Day",
      },
    },
    Metric: {
      description:
        "Single KPI tile. delta > 0 renders green ▲, delta < 0 renders red ▼, 0 or absent renders neutral.",
      props: z.object({
        label: z.string(),
        value: z.union([z.string(), z.number()]),
        prefix: z.string().optional(),
        suffix: z.string().optional(),
        delta: z.number().optional(),
      }),
      example: { label: "Revenue", value: 12480, prefix: "$", delta: 8.4 },
    },
    Badge: {
      description: "Inline pill for status / category.",
      props: z.object({ text: z.string(), tone: toneZ }),
      example: { text: "pass", tone: "success" },
    },
    Avatar: {
      description: "Circular avatar. Falls back to initials when src is missing.",
      props: z.object({
        src: z.string().optional(),
        alt: z.string().optional(),
        initials: z.string().max(3).optional(),
        size: z.number().int().positive().optional(),
      }),
      example: { initials: "DZ", size: 32 },
    },
    Icon: {
      description:
        'Inline SVG icon. Built-in names: check, x, info, warning, chevron, chevronUp, chevronRight, star, plus, minus, arrowUp, arrowDown. Unknown names fall back to "info".',
      props: z.object({
        name: z.string().optional(),
        size: z.number().int().positive().optional(),
      }),
      example: { name: "check", size: 14 },
    },
    Image: {
      description: "Image with optional caption.",
      props: z.object({
        src: z.string(),
        alt: z.string().optional(),
        width: z.number().int().positive().optional(),
        height: z.number().int().positive().optional(),
        caption: z.string().optional(),
      }),
      example: { src: "https://example.com/x.png", alt: "" },
    },

    // ── Interactive ────────────────────────────────────────────────
    Button: {
      description:
        'Click target. Fires "press" event — bind via the element\'s "on" field, e.g. `on: { press: { action: "approve", params: {...} } }`.',
      props: z.object({
        text: z.string(),
        variant: variantZ,
        disabled: z.boolean().optional(),
        iconLeft: z.string().optional(),
        iconRight: z.string().optional(),
      }),
      example: { text: "Approve", variant: "primary" },
    },
    Link: {
      description:
        'Hyperlink; fires "press" only when bound via the "on" field. Without a binding it navigates to href normally.',
      props: z.object({
        text: z.string(),
        href: z.string().optional(),
        external: z.boolean().optional(),
      }),
      example: { text: "Learn more", href: "https://example.com", external: true },
    },
    DropdownMenu: {
      description:
        'Menu of choices. Fires "select:<value>" — the value is inlined in the event name so each option can bind a distinct action.',
      props: z.object({
        label: z.string(),
        items: z.array(labelValueZ),
      }),
      example: {
        label: "Actions",
        items: [
          { label: "Rename", value: "rename" },
          { label: "Delete", value: "delete" },
        ],
      },
    },
    Popover: {
      description: "Click-to-reveal panel; anchor is the trigger label, children are the body.",
      slots: ["default"],
      props: z.object({ label: z.string(), open: z.boolean().optional() }),
      example: { label: "More", open: false },
    },
    Tooltip: {
      description: "Hover-to-reveal text on the child anchor.",
      slots: ["default"],
      props: z.object({ text: z.string() }),
      example: { text: "Descriptive hint" },
    },
    Rating: {
      description:
        'Star rating. Fires "rate:<n>" when the user clicks star n (1-indexed).',
      props: z.object({
        value: z.number().nonnegative().optional(),
        max: z.number().int().positive().optional(),
      }),
      example: { value: 4, max: 5 },
    },

    // ── User input ────────────────────────────────────────────────
    Input: {
      description:
        'Single-line text input. Fires "change:<value>" on blur — the value is inlined so a receiver can peek without params.',
      props: z.object({
        value: z.string().optional(),
        placeholder: z.string().optional(),
        type: inputTypeZ,
        label: z.string().optional(),
      }),
      example: { label: "Name", placeholder: "Enter your name" },
    },
    Textarea: {
      description: 'Multi-line text input. Fires "change:<value>" on blur.',
      props: z.object({
        value: z.string().optional(),
        placeholder: z.string().optional(),
        rows: z.number().int().positive().optional(),
        label: z.string().optional(),
      }),
      example: { label: "Comment", rows: 4 },
    },
    Select: {
      description: 'Dropdown selector. Fires "change:<value>".',
      props: z.object({
        value: z.string().optional(),
        label: z.string().optional(),
        options: z.array(labelValueZ),
      }),
      example: {
        label: "Country",
        options: [
          { label: "US", value: "us" },
          { label: "UK", value: "uk" },
        ],
      },
    },
    Checkbox: {
      description: 'Boolean checkbox. Fires "toggle:<bool>" on change.',
      props: z.object({ checked: z.boolean().optional(), label: z.string().optional() }),
      example: { label: "Accept terms", checked: false },
    },
    Radio: {
      description: 'Radio group — one selection at a time. Fires "select:<value>".',
      props: z.object({
        value: z.string().optional(),
        name: z.string().optional(),
        label: z.string().optional(),
        options: z.array(labelValueZ),
      }),
      example: {
        options: [
          { label: "Low", value: "low" },
          { label: "High", value: "high" },
        ],
      },
    },
    Toggle: {
      description: 'Switch. Fires "toggle:<bool>" on flip.',
      props: z.object({ checked: z.boolean().optional(), label: z.string().optional() }),
      example: { label: "Notifications" },
    },
    ToggleGroup: {
      description: 'Segmented control — one active option. Fires "select:<value>".',
      props: z.object({
        value: z.string().optional(),
        options: z.array(labelValueZ),
      }),
      example: {
        options: [
          { label: "Day", value: "d" },
          { label: "Week", value: "w" },
        ],
      },
    },
    Slider: {
      description: 'Range slider. Fires "change:<value>" as it moves.',
      props: z.object({
        value: z.number().optional(),
        min: z.number().optional(),
        max: z.number().optional(),
        step: z.number().positive().optional(),
        label: z.string().optional(),
      }),
      example: { label: "Threshold", min: 0, max: 100, value: 42 },
    },
    ButtonGroup: {
      description: 'Row of buttons. Fires "press:<value>" per click.',
      props: z.object({
        value: z.string().optional(),
        buttons: z.array(labelValueZ),
      }),
      example: {
        buttons: [
          { label: "Yes", value: "yes" },
          { label: "No", value: "no" },
        ],
      },
    },
    DatePicker: {
      description: 'ISO date input (YYYY-MM-DD). Fires "change:<value>".',
      props: z.object({
        value: z.string().optional(),
        label: z.string().optional(),
      }),
      example: { label: "When", value: "2024-01-15" },
    },

    // ── Feedback & status ─────────────────────────────────────────
    Alert: {
      description: "Attention banner with a tone (info/success/warning/danger).",
      props: z.object({
        tone: alertToneZ,
        title: z.string().optional(),
        text: z.string(),
      }),
      example: { tone: "warning", title: "Data required", text: "Provide the values to chart." },
    },
    Progress: {
      description: "Progress bar. value in 0..max; indeterminate hides the value and shows a shimmer.",
      props: z.object({
        value: z.number().nonnegative().optional(),
        max: z.number().positive().optional(),
        label: z.string().optional(),
        indeterminate: z.boolean().optional(),
      }),
      example: { label: "Uploading", value: 42, max: 100 },
    },
    Spinner: {
      description: "Inline loading spinner with optional label.",
      props: z.object({
        label: z.string().optional(),
        size: z.number().int().positive().optional(),
      }),
      example: { label: "Loading…", size: 16 },
    },
    Skeleton: {
      description: "Shimmering placeholder for content that hasn't arrived yet.",
      props: z.object({
        width: z.union([z.number(), z.string()]).optional(),
        height: z.union([z.number(), z.string()]).optional(),
        variant: z.enum(["line", "block", "circle"]).optional(),
      }),
      example: { width: "80%", height: 12, variant: "line" },
    },
  },
});

export type EmberSpec = (typeof catalog)["_specType"];
