import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
} from "react";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Contenteditable replacement for the composer textarea. Renders
 *  `@<path>` tokens as inline pills the moment they're followed by
 *  whitespace; backspacing right after a pill unwraps it back to
 *  raw text so the user can keep editing. The canonical value is
 *  still a plain string with literal `@<path>` tokens.
 */
export interface EditableInputHandle {
  focus: () => void;
  /** Caret offset in canonical-string space. */
  caret: () => number;
  /** Move caret to the end of the current content. */
  caretToEnd: () => void;
  /** Move caret to a specific canonical offset. The editor's DOM
   *  sync runs in a useEffect so call this *after* the next render
   *  via requestAnimationFrame if you've just updated the value. */
  setCaretAt: (offset: number) => void;
  /** Force the live DOM to match ``v``, bypassing the value-prop
   *  reconcile path. The Composer uses this when it consumes a typed
   *  trigger character (e.g. ``/`` flips to command mode + clears
   *  text) — React sees no state change in those cases, so the
   *  ``useEffect`` watcher wouldn't fire. */
  setValue: (v: string) => void;
}

interface Props {
  value: string;
  disabled?: boolean;
  placeholder?: string;
  className?: string;
  onValueChange: (next: string, caret: number) => void;
  onKeyDown?: (e: React.KeyboardEvent<HTMLDivElement>) => void;
  onPaste?: (e: React.ClipboardEvent<HTMLDivElement>) => void;
}

const PILL_ATTR = "data-pill-path";
const CODE_PILL_ATTR = "data-pill-code";
const CODE_PILL_LABEL_ATTR = "data-pill-label";

// "@/abs/path", "@./rel", "@bare", "@code:abc123" — token chars are
// non-whitespace. Code pills are distinguished by their "code:" prefix
// when serializing back from the DOM, but matching them here is the
// same — readValue/writeValue branch on whether the path starts with
// the marker.
const PILL_RE = /(?:^|(?<=\s))@(\S+)(?=\s|$)/;
const CODE_PILL_PREFIX = "code:";

/** Map of code-pill id → display label (e.g. "hello.py 9-11"). Set
 *  externally by Composer when it inserts a code pill. The renderer
 *  reads from this so it can show a meaningful label instead of a
 *  raw uuid. ``label`` is also stored on the DOM node so deep-copy /
 *  re-render scenarios work without external state. */
export const codePillLabels: Map<string, string> = new Map();

/** Walk the editor DOM and rebuild the canonical string. Pills emit
 *  their `data-pill-path` attribute back as `@<path>`. Line breaks
 *  (`<br>`) become `\n`. */
function pillToken(el: HTMLElement): string | null {
  if (el.hasAttribute(PILL_ATTR)) return `@${el.getAttribute(PILL_ATTR)}`;
  if (el.hasAttribute(CODE_PILL_ATTR))
    return `@${CODE_PILL_PREFIX}${el.getAttribute(CODE_PILL_ATTR)}`;
  return null;
}

function readValue(root: HTMLElement): string {
  let out = "";
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      out += node.textContent || "";
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as HTMLElement;
    const tok = pillToken(el);
    if (tok !== null) {
      out += tok;
      return;
    }
    if (el.tagName === "BR") {
      out += "\n";
      return;
    }
    if (el.tagName === "DIV" && el !== root) {
      // browsers wrap new lines in <div> on Enter
      if (out.length && !out.endsWith("\n")) out += "\n";
    }
    for (const child of Array.from(el.childNodes)) walk(child);
  };
  for (const child of Array.from(root.childNodes)) walk(child);
  return out;
}

/** Caret offset in the canonical string (matches readValue's
 *  rules). Uses the current selection. */
function readCaret(root: HTMLElement): number {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return 0;
  const range = sel.getRangeAt(0);
  if (!root.contains(range.endContainer)) return 0;

  let offset = 0;
  let found = false;

  const walk = (node: Node): void => {
    if (found) return;
    if (node === range.endContainer && node.nodeType === Node.TEXT_NODE) {
      offset += range.endOffset;
      found = true;
      return;
    }
    if (node.nodeType === Node.TEXT_NODE) {
      offset += (node.textContent || "").length;
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as HTMLElement;
    const tok = pillToken(el);
    if (tok !== null) {
      // Treat selection landing inside the pill as "end of pill".
      if (node === range.endContainer || el.contains(range.endContainer)) {
        offset += tok.length;
        found = true;
        return;
      }
      offset += tok.length;
      return;
    }
    if (el.tagName === "BR") {
      offset += 1;
      return;
    }
    for (const child of Array.from(el.childNodes)) {
      walk(child);
      if (found) return;
    }
  };

  for (const child of Array.from(root.childNodes)) {
    if (found) break;
    walk(child);
  }
  return offset;
}

/** Place the caret at the given canonical-string offset. */
function setCaret(root: HTMLElement, target: number) {
  let remaining = target;

  const dive = (node: Node): { node: Node; offset: number } | null => {
    if (node.nodeType === Node.TEXT_NODE) {
      const len = (node.textContent || "").length;
      if (remaining <= len) return { node, offset: remaining };
      remaining -= len;
      return null;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return null;
    const el = node as HTMLElement;
    const tok = pillToken(el);
    if (tok !== null) {
      if (remaining <= tok.length) {
        // Stand just after the pill node — the parent's child index
        // after this element. Selection is at element boundary.
        return { node: el.parentNode!, offset: childIndex(el) + 1 };
      }
      remaining -= tok.length;
      return null;
    }
    if (el.tagName === "BR") {
      if (remaining < 1) {
        return { node: el.parentNode!, offset: childIndex(el) };
      }
      remaining -= 1;
      return null;
    }
    for (const child of Array.from(el.childNodes)) {
      const hit = dive(child);
      if (hit) return hit;
    }
    return null;
  };

  const found = dive(root) || {
    node: root,
    offset: root.childNodes.length,
  };
  const range = document.createRange();
  range.setStart(found.node, found.offset);
  range.collapse(true);
  const sel = window.getSelection();
  sel?.removeAllRanges();
  sel?.addRange(range);
}

function childIndex(el: Element): number {
  let i = 0;
  let n = el.previousSibling;
  while (n) {
    i++;
    n = n.previousSibling;
  }
  return i;
}

/** HTML string for the given canonical value — used to decide
 *  whether the live DOM needs to be rebuilt. */
function renderHtml(value: string): string {
  const tmp = document.createElement("div");
  writeValue(tmp, value);
  return tmp.innerHTML;
}

/** Render the canonical string into the editor DOM (pills + text). */
function writeValue(root: HTMLElement, value: string) {
  // Build into a doc fragment so we touch the live DOM only once.
  const frag = document.createDocumentFragment();
  let rest = value;
  while (rest.length) {
    const m = rest.match(PILL_RE);
    if (!m || m.index === undefined) {
      appendText(frag, rest);
      break;
    }
    if (m.index > 0) appendText(frag, rest.slice(0, m.index));
    const inner = m[1];
    if (inner.startsWith(CODE_PILL_PREFIX)) {
      frag.appendChild(makeCodePill(inner.slice(CODE_PILL_PREFIX.length)));
    } else {
      frag.appendChild(makePill(inner));
    }
    rest = rest.slice(m.index + m[0].length);
  }
  root.replaceChildren(frag);
}

function appendText(parent: Node, text: string) {
  const parts = text.split("\n");
  parts.forEach((p, i) => {
    if (p) parent.appendChild(document.createTextNode(p));
    if (i < parts.length - 1) parent.appendChild(document.createElement("br"));
  });
}

/** Build the inline pill for a pasted code snippet. ``id`` is the
 *  opaque token used in the canonical text (``@code:<id>``). The
 *  display label is looked up in ``codePillLabels`` (a side-channel
 *  Map populated by Composer when it inserts the pill). */
function makeCodePill(id: string): HTMLSpanElement {
  const span = document.createElement("span");
  span.className = "file-pill code-pill inline-pill";
  span.setAttribute(CODE_PILL_ATTR, id);
  span.setAttribute("contenteditable", "false");
  const label = codePillLabels.get(id) || "code";
  span.setAttribute(CODE_PILL_LABEL_ATTR, label);
  span.title = label;
  const icon = document.createElement("span");
  icon.className = "file-pill-icon";
  icon.innerHTML = codePillSvg();
  const name = document.createElement("span");
  name.className = "file-pill-name";
  name.textContent = label;
  span.append(icon, name);
  return span;
}

function codePillSvg(): string {
  // Two-bracket "code" glyph — visually distinct from the file icon
  // so users instantly tell the pill is a snippet, not a path.
  return [
    `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true" style="display:block;flex-shrink:0">`,
    `<path d="M5.5 4L2.5 8L5.5 12" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" fill="none"/>`,
    `<path d="M10.5 4L13.5 8L10.5 12" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" fill="none"/>`,
    `</svg>`,
  ].join("");
}

function makePill(path: string): HTMLSpanElement {
  const span = document.createElement("span");
  span.className = "file-pill inline-pill";
  span.setAttribute(PILL_ATTR, path);
  span.setAttribute("contenteditable", "false");
  span.title = path;
  const name = path.split("/").pop() || path;
  // Same look as the .file-pill in ChatItems — icon + name. Render
  // the icon via the same React-rendered SVG would mean mounting a
  // React tree per pill; we inline a simpler glyph here to keep the
  // editor cheap.
  const icon = document.createElement("span");
  icon.className = "file-pill-icon";
  icon.innerHTML = pillSvg(name);
  const label = document.createElement("span");
  label.className = "file-pill-name";
  label.textContent = name;
  span.append(icon, label);
  return span;
}

/** Minimal inline file glyph — same visual budget as FileTypeIcon
 *  but built without React so the editor can mutate the DOM
 *  directly. */
function pillSvg(_name: string): string {
  return [
    `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true" style="display:block;flex-shrink:0">`,
    `<path d="M3.5 1.5h6L13 5v8a1.5 1.5 0 0 1-1.5 1.5h-8A1.5 1.5 0 0 1 2 13V3a1.5 1.5 0 0 1 1.5-1.5z" fill="currentColor" opacity="0.2"/>`,
    `<path d="M3.5 1.5h6L13 5v8a1.5 1.5 0 0 1-1.5 1.5h-8A1.5 1.5 0 0 1 2 13V3a1.5 1.5 0 0 1 1.5-1.5z" stroke="currentColor" stroke-width="1" fill="none"/>`,
    `<path d="M9.5 1.5V5H13" stroke="currentColor" stroke-width="1" fill="none"/>`,
    `<line x1="4" y1="9" x2="11" y2="9" stroke="currentColor" stroke-width="0.9"/>`,
    `<line x1="4" y1="10.6" x2="11" y2="10.6" stroke="currentColor" stroke-width="0.9"/>`,
    `<line x1="4" y1="12.2" x2="9" y2="12.2" stroke="currentColor" stroke-width="0.9"/>`,
    `</svg>`,
  ].join("");
}

export const EditableInput = forwardRef<EditableInputHandle, Props>(function EditableInput(
  { value, disabled, placeholder, className, onValueChange, onKeyDown, onPaste },
  ref,
) {
  const elRef = useRef<HTMLDivElement>(null);
  // Tracks the most recent value our own ``handleInput`` reported up
  // to the parent. When the next ``value`` prop comes back equal, we
  // know the DOM is already authoritative and skip the reconcile —
  // no DOM walk, no string compare, no risk of clobbering an
  // in-flight keystroke.
  const lastTypedRef = useRef<string | null>(null);

  // Sync external value → DOM only when `value` actually changes.
  // The previous implementation ran with no deps so it could catch
  // round-trip consumes (user typed ``/`` → parent flipped modes and
  // cleared text so React saw `value` stay the same while the DOM
  // still held the ``/``). That cost a DOM rewrite + caret reset on
  // every parent re-render and raced with rapid typing — the user
  // could see "test" land as "tets" because a stale-value render
  // would clobber the most recently typed character. The consume
  // case is now handled imperatively via ``setValue`` on the ref.
  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    // Value originated from our own input handler — the DOM is
    // already correct; don't even walk it.
    if (lastTypedRef.current === value) {
      lastTypedRef.current = null;
      return;
    }
    if (readValue(el) === value) return;
    const wasFocused = document.activeElement === el;
    const caret = wasFocused ? readCaret(el) : null;
    writeValue(el, value);
    if (caret !== null) setCaret(el, Math.min(caret, value.length));
  }, [value]);

  useImperativeHandle(
    ref,
    () => ({
      focus: () => elRef.current?.focus(),
      caret: () => (elRef.current ? readCaret(elRef.current) : 0),
      caretToEnd: () => {
        const el = elRef.current;
        if (!el) return;
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        const sel = window.getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      },
      setCaretAt: (offset: number) => {
        const el = elRef.current;
        if (!el) return;
        setCaret(el, offset);
      },
      setValue: (v: string) => {
        const el = elRef.current;
        if (!el) return;
        if (readValue(el) === v) return;
        const wasFocused = document.activeElement === el;
        const caret = wasFocused ? readCaret(el) : null;
        writeValue(el, v);
        if (caret !== null) setCaret(el, Math.min(caret, v.length));
      },
    }),
    [],
  );

  const handleInput = useCallback(() => {
    const el = elRef.current;
    if (!el) return;
    const next = readValue(el);
    // Plain typing fast path: no ``@`` → no pill normalization
    // could possibly be needed → skip the regex, the
    // ``renderHtml`` allocation, and the ``innerHTML`` compare.
    // Only when the value contains an unpilled ``@<token>`` do we
    // walk the normalize branch; that's the only case where the
    // browser-built DOM diverges from our canonical shape.
    if (next.includes("@") && PILL_RE.test(next)) {
      const caret = readCaret(el);
      const want = renderHtml(next);
      if (el.innerHTML !== want) {
        writeValue(el, next);
        setCaret(el, Math.min(caret, next.length));
      }
    }
    lastTypedRef.current = next;
    onValueChange(next, readCaret(el));
  }, [onValueChange]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const el = elRef.current;
      if (e.key === "Backspace" && el) {
        // Treat a pill as a single character: Backspace immediately
        // after a pill deletes the whole pill rather than expanding
        // it back into `@path` text. The old "unwrap on backspace"
        // behavior was clever but read as a cursor jump — most users
        // expect chip-style delete-as-unit.
        const sel = window.getSelection();
        if (sel && sel.rangeCount && sel.isCollapsed) {
          const r = sel.getRangeAt(0);
          // Caret is "right after" a pill in two DOM shapes:
          //   (a) startContainer === editor at offset N, and
          //       editor.childNodes[N-1] is the pill
          //   (b) startContainer is a text node directly following
          //       the pill, at offset 0
          let pill: HTMLElement | null = null;
          if (r.startContainer === el) {
            const node = el.childNodes[r.startOffset - 1] as HTMLElement | undefined;
            if (node?.nodeType === Node.ELEMENT_NODE && node && (node.hasAttribute?.(PILL_ATTR) || node.hasAttribute?.(CODE_PILL_ATTR))) {
              pill = node;
            }
          } else if (
            r.startContainer.nodeType === Node.TEXT_NODE &&
            r.startOffset === 0 &&
            r.startContainer.parentNode === el
          ) {
            const prev = (r.startContainer as Node).previousSibling as HTMLElement | null;
            if (prev?.nodeType === Node.ELEMENT_NODE && (prev.hasAttribute?.(PILL_ATTR) || prev.hasAttribute?.(CODE_PILL_ATTR))) {
              pill = prev;
            }
          }
          if (pill) {
            e.preventDefault();
            // Place the caret where the pill used to start, then drop
            // the pill. Adjacent text nodes around it stay put.
            const range = document.createRange();
            const parent = pill.parentNode as Node;
            range.setStartBefore(pill);
            range.collapse(true);
            pill.remove();
            sel.removeAllRanges();
            sel.addRange(range);
            // Trigger the normalization/onValueChange pipeline so the
            // canonical string reflects the deletion.
            handleInput();
            // parent may have left two adjacent text nodes — merge so
            // future caret math works without surprises.
            (parent as Element).normalize?.();
            return;
          }
        }
      }
      onKeyDown?.(e);
    },
    [handleInput, onKeyDown],
  );

  return (
    <div
      ref={elRef}
      className={`composer-editable${className ? " " + className : ""}`}
      contentEditable={!disabled}
      role="textbox"
      aria-multiline="true"
      data-placeholder={placeholder}
      spellCheck
      suppressContentEditableWarning
      onInput={handleInput}
      onKeyDown={handleKeyDown}
      onPaste={onPaste}
    />
  );
});
