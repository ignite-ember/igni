/**
 * JsonRenderView — mounts a json-render spec inside the chat stream.
 *
 * The BE's ``visualizer`` sub-agent produces a flat-tree spec
 * (``{ root, elements }``) and pushes it via
 * ``PushNotification(channel="visualization")``. App.tsx routes those
 * pushes to a ``visualization`` chat item, ChatItems renders that item
 * by mounting this component.
 *
 * Architecture:
 *
 * - **Catalog is the single source of truth.** All 41 component
 *   definitions (Zod prop schemas + descriptions + examples) live in
 *   ``jsonRender/catalog.ts``. This file consumes the catalog via
 *   ``defineRegistry(catalog, { components: {...} })`` — one place
 *   to change, one place the LLM prompt is generated from.
 * - **The @json-render/react library owns rendering.** ``<Renderer>``
 *   walks the spec; ``JSONUIProvider`` supplies state + action
 *   handlers. We provide React implementations for each catalog entry
 *   and let the library do the rest.
 * - **Actions round-trip through the BE.** The ``handlers`` proxy on
 *   ``JSONUIProvider`` forwards every fired action to the parent
 *   ``onDispatchAction`` callback, which App.tsx wires to the
 *   ``dispatch_visualization_action`` RPC.
 */

import {
  Renderer,
  JSONUIProvider,
  defineRegistry,
  type ComponentRegistry,
} from "@json-render/react";
import type { Spec } from "@json-render/core";
import { memo, useCallback, useMemo, useState } from "react";
import type React from "react";
import { catalog } from "./jsonRender/catalog";
import "./JsonRenderView.css";

// ── Chart primitives (kept in-file — no charting lib pull-in) ──────

type SeriesPoint = { x: string | number; y: number };

function toStrRecord(input: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (!input || typeof input !== "object") return out;
  for (const [k, v] of Object.entries(input as Record<string, unknown>)) {
    out[k] = v == null ? "" : String(v);
  }
  return out;
}

function LineChart({
  data,
  yPrefix = "",
  ySuffix = "",
  width = 640,
  height = 220,
}: {
  data: SeriesPoint[];
  yPrefix?: string;
  ySuffix?: string;
  width?: number;
  height?: number;
}) {
  if (!data.length) return <div className="jr-empty">no data</div>;
  const padL = 46,
    padR = 12,
    padT = 12,
    padB = 26;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const ys = data.map((d) => d.y);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const yRange = yMax - yMin || 1;
  const xStep = data.length > 1 ? innerW / (data.length - 1) : 0;
  const points = data.map((d, i) => {
    const x = padL + i * xStep;
    const y = padT + innerH - ((d.y - yMin) / yRange) * innerH;
    return `${x},${y}`;
  });
  const yTickVals = Array.from({ length: 5 }, (_, i) => yMin + (yRange * i) / 4);
  return (
    <svg
      className="jr-linechart"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="line chart"
    >
      {yTickVals.map((v, i) => {
        const y = padT + innerH - ((v - yMin) / yRange) * innerH;
        return (
          <g key={`yt-${i}`}>
            <line x1={padL} x2={width - padR} y1={y} y2={y} className="jr-linechart-grid" />
            <text x={padL - 6} y={y + 3} className="jr-linechart-ytick">
              {yPrefix}
              {Number.isInteger(v) ? v : v.toFixed(2)}
              {ySuffix}
            </text>
          </g>
        );
      })}
      {data.map((d, i) => {
        if (i % Math.max(1, Math.ceil(data.length / 8)) !== 0) return null;
        const x = padL + i * xStep;
        return (
          <text
            key={`xt-${i}`}
            x={x}
            y={height - 8}
            className="jr-linechart-xtick"
            textAnchor="middle"
          >
            {String(d.x)}
          </text>
        );
      })}
      <polyline points={points.join(" ")} className="jr-linechart-line" />
      {data.map((_d, i) => {
        const [x, y] = points[i].split(",").map(Number);
        return <circle key={`p-${i}`} cx={x} cy={y} r={2.5} className="jr-linechart-dot" />;
      })}
    </svg>
  );
}

function BarChart({
  data,
  yPrefix = "",
  ySuffix = "",
  width = 640,
  height = 220,
}: {
  data: SeriesPoint[];
  yPrefix?: string;
  ySuffix?: string;
  width?: number;
  height?: number;
}) {
  if (!data.length) return <div className="jr-empty">no data</div>;
  const padL = 46,
    padR = 12,
    padT = 12,
    padB = 26;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const ys = data.map((d) => d.y);
  const yMax = Math.max(...ys, 0);
  const yMin = Math.min(...ys, 0);
  const yRange = yMax - yMin || 1;
  const bandW = innerW / data.length;
  const barW = Math.max(2, bandW * 0.6);
  return (
    <svg
      className="jr-barchart"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="bar chart"
    >
      {[0, 0.5, 1].map((f, i) => {
        const v = yMin + yRange * f;
        const y = padT + innerH - ((v - yMin) / yRange) * innerH;
        return (
          <g key={`yt-${i}`}>
            <line x1={padL} x2={width - padR} y1={y} y2={y} className="jr-barchart-grid" />
            <text x={padL - 6} y={y + 3} className="jr-barchart-ytick">
              {yPrefix}
              {Number.isInteger(v) ? v : v.toFixed(2)}
              {ySuffix}
            </text>
          </g>
        );
      })}
      {data.map((d, i) => {
        const x = padL + i * bandW + (bandW - barW) / 2;
        const zero = padT + innerH - ((0 - yMin) / yRange) * innerH;
        const top = padT + innerH - ((d.y - yMin) / yRange) * innerH;
        const y = Math.min(zero, top);
        const h = Math.abs(zero - top);
        return (
          <g key={`b-${i}`}>
            <rect x={x} y={y} width={barW} height={h} className="jr-barchart-bar" />
            <text x={x + barW / 2} y={height - 8} className="jr-barchart-xtick" textAnchor="middle">
              {String(d.x)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

// ── Icon set (small inline SVGs so we don't pull an icon lib) ──────

const ICONS: Record<string, string> = {
  check: "M4 8l3 3 5-6",
  x: "M4 4l8 8M12 4l-8 8",
  info: "M8 5v3M8 11h.01M2 8a6 6 0 1 0 12 0 6 6 0 0 0-12 0",
  warning: "M8 3l6 10H2L8 3zM8 7v3M8 11h.01",
  chevron: "M4 6l4 4 4-4",
  chevronUp: "M4 10l4-4 4 4",
  chevronRight: "M6 4l4 4-4 4",
  star: "M8 2l1.8 3.7 4 .6-2.9 2.9.7 4L8 11.3l-3.6 1.9.7-4L2.2 6.3l4-.6z",
  plus: "M8 3v10M3 8h10",
  minus: "M3 8h10",
  arrowUp: "M8 3l4 4H10v6H6V7H4z",
  arrowDown: "M8 13l-4-4h2V3h4v6h2z",
};

function InlineIcon({ name, size = 14 }: { name: string; size?: number }) {
  const d = ICONS[name] ?? ICONS.info;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={d} />
    </svg>
  );
}

// ── Registry — catalog + React implementations ─────────────────────

const { registry } = defineRegistry(catalog, {
  components: {
    Stack: ({ props, children }) => (
      <div className={`jr-stack jr-gap-${props.gap ?? "md"}`}>{children}</div>
    ),

    Grid: ({ props, children }) => {
      const cols = Math.max(1, props.columns);
      return (
        <div
          className={`jr-grid jr-gap-${props.gap ?? "md"}`}
          style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
        >
          {children}
        </div>
      );
    },

    Card: ({ props, children }) => (
      <div className="jr-card">
        {(props.title || props.subtitle) && (
          <div className="jr-card-header">
            {props.title && <div className="jr-card-title">{props.title}</div>}
            {props.subtitle && (
              <div className="jr-card-subtitle">{props.subtitle}</div>
            )}
          </div>
        )}
        <div className="jr-card-body">{children}</div>
      </div>
    ),

    Carousel: ({ props, children }) => {
      const slides = Array.isArray(children) ? children : children ? [children] : [];
      const [idx, setIdx] = useState(props.activeIndex ?? 0);
      const total = slides.length || 1;
      const clamped = ((idx % total) + total) % total;
      return (
        <div className="jr-carousel">
          <div className="jr-carousel-viewport">{slides[clamped] ?? null}</div>
          {total > 1 && (
            <div className="jr-carousel-controls">
              <button
                type="button"
                className="jr-btn jr-btn-ghost"
                onClick={() => setIdx((n) => n - 1)}
                aria-label="Previous slide"
              >
                <InlineIcon name="chevron" />
              </button>
              <div className="jr-carousel-dots">
                {Array.from({ length: total }, (_, i) => (
                  <button
                    key={i}
                    type="button"
                    className={`jr-carousel-dot ${i === clamped ? "jr-carousel-dot-on" : ""}`}
                    onClick={() => setIdx(i)}
                    aria-label={`Slide ${i + 1}`}
                  />
                ))}
              </div>
              <button
                type="button"
                className="jr-btn jr-btn-ghost"
                onClick={() => setIdx((n) => n + 1)}
                aria-label="Next slide"
              >
                <InlineIcon name="chevronRight" />
              </button>
            </div>
          )}
        </div>
      );
    },

    Accordion: ({ props }) => {
      const items = props.items;
      const allowMultiple = props.allowMultiple ?? false;
      const [open, setOpen] = useState<Set<number>>(new Set([0]));
      const toggle = (i: number) => {
        setOpen((prev) => {
          const next = new Set(prev);
          if (next.has(i)) next.delete(i);
          else {
            if (!allowMultiple) next.clear();
            next.add(i);
          }
          return next;
        });
      };
      return (
        <div className="jr-accordion">
          {items.map((it, i) => {
            const isOpen = open.has(i);
            return (
              <div key={i} className={`jr-accordion-item ${isOpen ? "jr-open" : ""}`}>
                <button
                  type="button"
                  className="jr-accordion-header"
                  onClick={() => toggle(i)}
                  aria-expanded={isOpen}
                >
                  <span>{it.title}</span>
                  <InlineIcon name={isOpen ? "chevronUp" : "chevron"} />
                </button>
                {isOpen && <div className="jr-accordion-body">{it.body ?? ""}</div>}
              </div>
            );
          })}
        </div>
      );
    },

    Tabs: ({ props, children }) => {
      const tabs = props.tabs;
      const panels = Array.isArray(children) ? children : children ? [children] : [];
      const [active, setActive] = useState(props.activeIndex ?? 0);
      const n = Math.max(tabs.length, panels.length);
      if (!n) return null;
      return (
        <div className="jr-tabs">
          <div className="jr-tabs-strip" role="tablist">
            {Array.from({ length: n }, (_, i) => (
              <button
                key={i}
                type="button"
                role="tab"
                aria-selected={i === active}
                className={`jr-tab ${i === active ? "jr-tab-on" : ""}`}
                onClick={() => setActive(i)}
              >
                {tabs[i]?.label ?? `Tab ${i + 1}`}
              </button>
            ))}
          </div>
          <div className="jr-tabs-panel">{panels[active] ?? null}</div>
        </div>
      );
    },

    Dialog: ({ props, children }) => {
      if (props.open === false) return null;
      return (
        <div className="jr-dialog">
          {props.title && <div className="jr-dialog-header">{props.title}</div>}
          <div className="jr-dialog-body">{children}</div>
        </div>
      );
    },

    Drawer: ({ props, children }) => {
      if (props.open === false) return null;
      return (
        <div className="jr-drawer">
          {props.title && <div className="jr-drawer-header">{props.title}</div>}
          <div className="jr-drawer-body">{children}</div>
        </div>
      );
    },

    Heading: ({ props }) => {
      const level = Math.min(4, Math.max(1, props.level ?? 2));
      const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4";
      return <Tag className={`jr-heading jr-h${level}`}>{props.text}</Tag>;
    },

    Text: ({ props }) => {
      const cls = [
        "jr-text",
        props.muted ? "jr-text-muted" : "",
        props.mono ? "jr-text-mono" : "",
      ]
        .filter(Boolean)
        .join(" ");
      return <div className={cls}>{props.text}</div>;
    },

    Table: ({ props }) => {
      const cols = props.columns;
      const rows = props.rows;
      return (
        <table className="jr-table">
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c.key} style={{ textAlign: c.align ?? "left" }}>
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => {
              const rec = toStrRecord(row);
              return (
                <tr key={i}>
                  {cols.map((c) => (
                    <td key={c.key} style={{ textAlign: c.align ?? "left" }}>
                      {rec[c.key] ?? ""}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      );
    },

    LineGraph: ({ props }) => (
      <div className="jr-chart">
        <LineChart
          data={props.data}
          yPrefix={props.yPrefix}
          ySuffix={props.ySuffix}
        />
        {(props.xLabel || props.yLabel) && (
          <div className="jr-chart-axes">
            {props.yLabel && <span className="jr-chart-y">{props.yLabel}</span>}
            {props.xLabel && <span className="jr-chart-x">{props.xLabel}</span>}
          </div>
        )}
      </div>
    ),

    BarGraph: ({ props }) => (
      <div className="jr-chart">
        <BarChart
          data={props.data}
          yPrefix={props.yPrefix}
          ySuffix={props.ySuffix}
        />
        {(props.xLabel || props.yLabel) && (
          <div className="jr-chart-axes">
            {props.yLabel && <span className="jr-chart-y">{props.yLabel}</span>}
            {props.xLabel && <span className="jr-chart-x">{props.xLabel}</span>}
          </div>
        )}
      </div>
    ),

    Metric: ({ props }) => {
      const { label, value, prefix, suffix, delta } = props;
      const deltaTone =
        typeof delta === "number"
          ? delta > 0
            ? "up"
            : delta < 0
              ? "down"
              : "flat"
          : "";
      return (
        <div className="jr-metric">
          <div className="jr-metric-label">{label}</div>
          <div className="jr-metric-value">
            {prefix ?? ""}
            {value}
            {suffix ?? ""}
          </div>
          {typeof delta === "number" && (
            <div className={`jr-metric-delta jr-metric-${deltaTone}`}>
              {delta > 0 ? "▲" : delta < 0 ? "▼" : "•"} {Math.abs(delta)}%
            </div>
          )}
        </div>
      );
    },

    Badge: ({ props }) => (
      <span className={`jr-badge jr-badge-${props.tone ?? "neutral"}`}>
        {props.text}
      </span>
    ),

    Avatar: ({ props }) => {
      const { src, alt, initials, size = 32 } = props;
      if (src) {
        return (
          <img
            className="jr-avatar"
            src={src}
            alt={alt ?? ""}
            width={size}
            height={size}
          />
        );
      }
      return (
        <span
          className="jr-avatar jr-avatar-initials"
          style={{ width: size, height: size, fontSize: Math.round(size * 0.4) }}
          aria-label={alt ?? initials ?? ""}
        >
          {(initials ?? "?").slice(0, 2).toUpperCase()}
        </span>
      );
    },

    Icon: ({ props }) => (
      <InlineIcon name={props.name ?? "info"} size={props.size ?? 14} />
    ),

    Image: ({ props }) => {
      const { src, alt, width, height, caption } = props;
      if (!src) return null;
      return (
        <figure className="jr-image">
          <img src={src} alt={alt ?? ""} width={width} height={height} />
          {caption && <figcaption className="jr-image-caption">{caption}</figcaption>}
        </figure>
      );
    },

    Button: ({ props, emit }) => {
      const { text, variant = "primary", disabled, iconLeft, iconRight } = props;
      return (
        <button
          type="button"
          className={`jr-btn jr-btn-${variant}`}
          disabled={disabled}
          onClick={() => emit("press")}
        >
          {iconLeft && <InlineIcon name={iconLeft} />}
          <span>{text}</span>
          {iconRight && <InlineIcon name={iconRight} />}
        </button>
      );
    },

    Link: ({ props, emit, on }) => {
      const { text, href, external } = props;
      const handle = on("press");
      return (
        <a
          className="jr-link"
          href={href ?? "#"}
          target={external ? "_blank" : undefined}
          rel={external ? "noreferrer noopener" : undefined}
          onClick={(e) => {
            // Only hijack the click when a press action is bound;
            // otherwise let the browser follow the href normally.
            if (handle.bound) {
              e.preventDefault();
              emit("press");
            }
          }}
        >
          {text}
        </a>
      );
    },

    DropdownMenu: ({ props, emit }) => {
      const [open, setOpen] = useState(false);
      return (
        <div className={`jr-dropdown ${open ? "jr-dropdown-open" : ""}`}>
          <button
            type="button"
            className="jr-btn jr-btn-secondary"
            onClick={() => setOpen((v) => !v)}
          >
            <span>{props.label}</span>
            <InlineIcon name={open ? "chevronUp" : "chevron"} />
          </button>
          {open && (
            <div className="jr-dropdown-menu" role="menu">
              {props.items.map((it, i) => (
                <button
                  key={i}
                  type="button"
                  className="jr-dropdown-item"
                  role="menuitem"
                  onClick={() => {
                    setOpen(false);
                    emit(`select:${it.value}`);
                  }}
                >
                  {it.label}
                </button>
              ))}
            </div>
          )}
        </div>
      );
    },

    Popover: ({ props, children }) => {
      const [open, setOpen] = useState(props.open ?? false);
      return (
        <div className={`jr-popover ${open ? "jr-popover-open" : ""}`}>
          <button
            type="button"
            className="jr-btn jr-btn-ghost"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {props.label}
          </button>
          {open && <div className="jr-popover-body">{children}</div>}
        </div>
      );
    },

    Tooltip: ({ props, children }) => (
      <span className="jr-tooltip">
        <span className="jr-tooltip-anchor">{children}</span>
        <span className="jr-tooltip-bubble">{props.text}</span>
      </span>
    ),

    Rating: ({ props, emit }) => {
      const max = props.max ?? 5;
      const val = Math.max(0, Math.min(max, props.value ?? 0));
      return (
        <div className="jr-rating" role="radiogroup" aria-label="rating">
          {Array.from({ length: max }, (_, i) => {
            const filled = i < Math.round(val);
            return (
              <button
                key={i}
                type="button"
                role="radio"
                aria-checked={filled}
                className={`jr-rating-star ${filled ? "jr-rating-star-on" : ""}`}
                onClick={() => emit(`rate:${i + 1}`)}
              >
                <InlineIcon name="star" />
              </button>
            );
          })}
        </div>
      );
    },

    Input: ({ props, emit }) => {
      const { value, placeholder, type = "text", label } = props;
      return (
        <label className="jr-field">
          {label && <span className="jr-field-label">{label}</span>}
          <input
            className="jr-input"
            type={type}
            defaultValue={value ?? ""}
            placeholder={placeholder ?? ""}
            onBlur={(e) => emit(`change:${e.currentTarget.value}`)}
          />
        </label>
      );
    },

    Textarea: ({ props, emit }) => {
      const { value, placeholder, rows = 3, label } = props;
      return (
        <label className="jr-field">
          {label && <span className="jr-field-label">{label}</span>}
          <textarea
            className="jr-textarea"
            defaultValue={value ?? ""}
            placeholder={placeholder ?? ""}
            rows={rows}
            onBlur={(e) => emit(`change:${e.currentTarget.value}`)}
          />
        </label>
      );
    },

    Select: ({ props, emit }) => {
      const { value, label, options } = props;
      return (
        <label className="jr-field">
          {label && <span className="jr-field-label">{label}</span>}
          <select
            className="jr-select"
            defaultValue={value ?? ""}
            onChange={(e) => emit(`change:${e.currentTarget.value}`)}
          >
            {options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
      );
    },

    Checkbox: ({ props, emit }) => (
      <label className="jr-checkbox">
        <input
          type="checkbox"
          defaultChecked={props.checked ?? false}
          onChange={(e) => emit(`toggle:${e.currentTarget.checked}`)}
        />
        <span>{props.label ?? ""}</span>
      </label>
    ),

    Radio: ({ props, emit }) => (
      <div className="jr-radio-group" role="radiogroup">
        {props.options.map((o) => (
          <label key={o.value} className="jr-radio">
            <input
              type="radio"
              name={props.name ?? "radio"}
              value={o.value}
              defaultChecked={props.value === o.value}
              onChange={() => emit(`select:${o.value}`)}
            />
            <span>{o.label}</span>
          </label>
        ))}
      </div>
    ),

    Toggle: ({ props, emit }) => {
      const [checked, setChecked] = useState(props.checked ?? false);
      return (
        <button
          type="button"
          className={`jr-toggle ${checked ? "jr-toggle-on" : ""}`}
          role="switch"
          aria-checked={checked}
          onClick={() => {
            const next = !checked;
            setChecked(next);
            emit(`toggle:${next}`);
          }}
        >
          <span className="jr-toggle-thumb" />
          {props.label && (
            <span className="jr-toggle-label">{props.label}</span>
          )}
        </button>
      );
    },

    ToggleGroup: ({ props, emit }) => {
      const [val, setVal] = useState(props.value ?? "");
      return (
        <div className="jr-togglegroup" role="tablist">
          {props.options.map((o) => (
            <button
              key={o.value}
              type="button"
              role="tab"
              aria-selected={val === o.value}
              className={`jr-togglegroup-item ${val === o.value ? "jr-on" : ""}`}
              onClick={() => {
                setVal(o.value);
                emit(`select:${o.value}`);
              }}
            >
              {o.label}
            </button>
          ))}
        </div>
      );
    },

    Slider: ({ props, emit }) => {
      const { value, min = 0, max = 100, step = 1, label } = props;
      return (
        <label className="jr-field">
          {label && (
            <span className="jr-field-label">
              {label} ({value ?? min})
            </span>
          )}
          <input
            className="jr-slider"
            type="range"
            min={min}
            max={max}
            step={step}
            defaultValue={value ?? min}
            onChange={(e) => emit(`change:${e.currentTarget.value}`)}
          />
        </label>
      );
    },

    ButtonGroup: ({ props, emit }) => (
      <div className="jr-btngroup">
        {props.buttons.map((b) => (
          <button
            key={b.value}
            type="button"
            className={`jr-btngroup-btn ${props.value === b.value ? "jr-on" : ""}`}
            onClick={() => emit(`press:${b.value}`)}
          >
            {b.label}
          </button>
        ))}
      </div>
    ),

    DatePicker: ({ props, emit }) => (
      <label className="jr-field">
        {props.label && (
          <span className="jr-field-label">{props.label}</span>
        )}
        <input
          className="jr-input"
          type="date"
          defaultValue={props.value ?? ""}
          onChange={(e) => emit(`change:${e.currentTarget.value}`)}
        />
      </label>
    ),

    Alert: ({ props }) => (
      <div className={`jr-alert jr-alert-${props.tone}`}>
        {props.title && <div className="jr-alert-title">{props.title}</div>}
        <div className="jr-alert-body">{props.text}</div>
      </div>
    ),

    Progress: ({ props }) => {
      const { value = 0, max = 100, label, indeterminate } = props;
      const clamped = Math.max(0, Math.min(max, value));
      const pct = (clamped / max) * 100;
      return (
        <div className="jr-progress">
          {label && <div className="jr-progress-label">{label}</div>}
          <div className="jr-progress-track">
            <div
              className={`jr-progress-bar ${indeterminate ? "jr-progress-indet" : ""}`}
              style={{ width: indeterminate ? "40%" : `${pct}%` }}
            />
          </div>
        </div>
      );
    },

    Spinner: ({ props }) => {
      const size = props.size ?? 16;
      return (
        <span className="jr-spinner" aria-live="polite">
          <span
            className="jr-spinner-ring"
            style={{ width: size, height: size }}
            aria-hidden="true"
          />
          {props.label && (
            <span className="jr-spinner-label">{props.label}</span>
          )}
        </span>
      );
    },

    Skeleton: ({ props }) => {
      const { width, height, variant } = props;
      const style: React.CSSProperties = {
        width: width ?? "100%",
        height: height ?? (variant === "circle" ? 32 : variant === "block" ? 60 : 12),
        borderRadius: variant === "circle" ? "50%" : variant === "block" ? 8 : 6,
      };
      return <span className="jr-skeleton" style={style} aria-hidden="true" />;
    },
  },
});

// The fallback receives the raw ``ComponentRenderProps`` shape (not
// the catalog-typed ``ComponentContext``) because it's registered on
// the Renderer directly, not through defineRegistry. That shape still
// carries ``element`` (with type + props) which we use to name the
// missing component in the placeholder.
const fallback: ComponentRegistry[string] = ({ element }) => (
  <div className="jr-unknown">
    Unknown component <code>{String(element?.type ?? "?")}</code>
  </div>
);

// ── Action wire ─────────────────────────────────────────────────────

export type DispatchAction = (
  name: string,
  params: Record<string, unknown>,
) => Promise<unknown>;

interface JsonRenderViewProps {
  spec: Spec;
  title?: string;
  sourceAgent?: string;
  onDispatchAction?: DispatchAction;
}

function JsonRenderViewInner({
  spec,
  title,
  sourceAgent,
  onDispatchAction,
}: JsonRenderViewProps) {
  const dispatch = useCallback(
    async (name: string, params: Record<string, unknown>) => {
      if (onDispatchAction) return onDispatchAction(name, params);
      // eslint-disable-next-line no-console
      console.debug("[json-render] unhandled action", name, params);
      return undefined;
    },
    [onDispatchAction],
  );

  // JSONUIProvider takes ``handlers: Record<string, fn>``. We don't
  // know action names ahead of time (the agent invents them per spec),
  // so we hand the renderer a Proxy that returns the same generic
  // dispatcher for any key. ``has`` claims every key so no action is
  // ever treated as "unhandled" and dropped by the renderer.
  const handlers = useMemo(
    () =>
      new Proxy<Record<string, (params: Record<string, unknown>) => Promise<unknown>>>(
        {},
        {
          get(_target, prop) {
            if (typeof prop !== "string") return undefined;
            return (params: Record<string, unknown>) => dispatch(prop, params ?? {});
          },
          has() {
            return true;
          },
        },
      ),
    [dispatch],
  );

  return (
    <div className="jr-view">
      {(title || sourceAgent) && (
        <div className="jr-view-header">
          {title && <div className="jr-view-title">{title}</div>}
          {sourceAgent && <div className="jr-view-source">via {sourceAgent}</div>}
        </div>
      )}
      <JSONUIProvider registry={registry} handlers={handlers}>
        <Renderer spec={spec} registry={registry} fallback={fallback} />
      </JSONUIProvider>
    </div>
  );
}

/** Memoize on the spec identity — the wire delivers the whole spec in
 *  one push, so the object reference is a valid change signal. Without
 *  memo, a chat re-render (new tool card arriving, sibling item update)
 *  would re-mount the whole Renderer and any internal state it holds. */
export const JsonRenderView = memo(JsonRenderViewInner);
