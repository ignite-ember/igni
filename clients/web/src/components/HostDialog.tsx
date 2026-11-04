/**
 * An extension's dialog — `host.ui.show_dialog`, drawn by igni.
 *
 * The dialog is the window model's `ui.dialog`; this draws it and hands
 * back the person's answer, which the extension that asked is waiting
 * on: `{button, value}`, or `null` when it is dismissed (Escape, the
 * backdrop). Text only — a title, a body, up to four buttons and an
 * optional line of input — so an extension can ask, and never paint.
 * It is labelled with the extension's name, the way its chat messages
 * are, so nobody mistakes it for igni asking.
 */

import { useEffect, useState } from "react";

import type { Dialog } from "../clientState";

interface Button {
  id: string;
  label: string;
  primary?: boolean;
}

interface Spec {
  title: string;
  body?: string;
  buttons?: Button[];
  input?: { label?: string; placeholder?: string; value?: string } | null;
}

export interface DialogAnswer {
  button: string;
  value: string | null;
}

export function HostDialog({
  dialog,
  sourceName,
  onAnswer,
}: {
  dialog: Dialog;
  /** The asking extension's display name. */
  sourceName: string;
  onAnswer: (answer: DialogAnswer | null) => void;
}) {
  const spec = dialog.spec as unknown as Spec;
  const buttons = spec.buttons?.length
    ? spec.buttons
    : [{ id: "ok", label: "OK", primary: true }];
  // The line being typed, before a button submits it as the answer.
  const [value, setValue] = useState(spec.input?.value ?? "");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onAnswer(null);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onAnswer]);

  const answer = (button: string) =>
    onAnswer({ button, value: spec.input ? value : null });
  const primary = buttons.find((b) => b.primary) ?? buttons[buttons.length - 1];

  return (
    <div
      className="update-prompt-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onAnswer(null);
      }}
    >
      <div
        className="update-prompt"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${dialog.id}-title`}
      >
        <div className="host-dialog-from">from {sourceName}</div>
        <div className="update-prompt-title" id={`${dialog.id}-title`}>
          {spec.title}
        </div>
        {spec.body && <div className="update-prompt-body">{spec.body}</div>}
        {spec.input && (
          <label className="host-dialog-field">
            {spec.input.label && <span>{spec.input.label}</span>}
            <input
              className="dir-path-input"
              value={value}
              placeholder={spec.input.placeholder}
              spellCheck={false}
              autoFocus
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") answer(primary.id);
              }}
            />
          </label>
        )}
        <div className="update-prompt-actions">
          {buttons.map((b) => (
            <button
              key={b.id}
              type="button"
              className={b.primary ? "btn btn-primary" : "btn"}
              onClick={() => answer(b.id)}
            >
              {b.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
