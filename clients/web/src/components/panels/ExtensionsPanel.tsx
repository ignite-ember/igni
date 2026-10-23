import { useMemo, useState } from "react";
import type { IgniClient } from "../../protocol/client";
import type { ExtensionSummary } from "../../extensions/ExtensionPage";
import { Drawer } from "./Drawer";

/**
 * What is installed, why it is not working, and what it can be set to.
 *
 * `contributes.settings` has been modelled, validated, stored beside
 * the bundle and carried on `list_extensions` for a while — and the
 * only way to change one was to call `set_extension_settings` by
 * hand. A declared setting nobody can reach is a declaration, not a
 * setting.
 *
 * It is also the one place the host says what state an extension is
 * in. Before this, a FAILED extension showed as a sidebar entry that
 * opened onto its own reason; there was nowhere to see the three
 * installed extensions together and notice that one of them had been
 * down since boot.
 */

/** One setting's current value — what the control is bound to. */
type Value = string | number | boolean;

/** The four `Setting.type` values the manifest allows. */
const KNOWN_TYPES = new Set(["string", "number", "boolean", "enum"]);

/**
 * What the form should start at.
 *
 * `values` already has defaults filled in by the backend, so this is
 * a fallback for a setting added by an upgrade that has not been
 * saved since — not the normal path.
 */
function initial(extension: ExtensionSummary): Record<string, Value> {
  const start: Record<string, Value> = {};
  for (const setting of extension.settings) {
    const held = extension.values[setting.key] ?? setting.default;
    if (setting.type === "boolean") start[setting.key] = held === true;
    else if (setting.type === "number") start[setting.key] = Number(held ?? 0);
    else
      start[setting.key] =
        held === undefined || held === null ? "" : String(held);
  }
  return start;
}

function sameValues(
  a: Record<string, Value>,
  b: Record<string, Value>,
): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const key of keys) if (a[key] !== b[key]) return false;
  return true;
}

/** A state, as a word and a tone the stylesheet already has. */
function stateClass(state: string): string {
  if (state === "ready") return "ext-state ext-state-ready";
  if (state === "failed") return "ext-state ext-state-failed";
  return "ext-state ext-state-busy";
}

function SettingsForm({
  extension,
  onSave,
}: {
  extension: ExtensionSummary;
  onSave: (values: Record<string, Value>) => Promise<void>;
}) {
  const saved = useMemo(() => initial(extension), [extension]);
  const [draft, setDraft] = useState<Record<string, Value>>(saved);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const set = (key: string, value: Value) =>
    setDraft((current) => ({ ...current, [key]: value }));

  // A number input that has been cleared reads as `NaN`, and sending
  // that would store `null` and look like the setting was reset.
  // Named rather than silently dropped: the administrator typed
  // something, and the form should say which field it cannot use.
  const broken = extension.settings
    .filter((s) => s.type === "number" && Number.isNaN(draft[s.key]))
    .map((s) => s.label);

  const dirty = !sameValues(draft, saved);

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await onSave(draft);
    } catch (problem: unknown) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ext-settings">
      {extension.settings.map((setting) => (
        <label className="ext-setting" key={setting.key}>
          <span className="ext-setting-label">{setting.label}</span>
          {setting.type === "boolean" && (
            <input
              type="checkbox"
              checked={draft[setting.key] === true}
              onChange={(e) => set(setting.key, e.target.checked)}
            />
          )}
          {setting.type === "number" && (
            <input
              className="panel-input"
              type="number"
              value={
                Number.isNaN(draft[setting.key])
                  ? ""
                  : String(draft[setting.key])
              }
              onChange={(e) => set(setting.key, e.target.valueAsNumber)}
            />
          )}
          {setting.type === "string" && (
            <input
              className="panel-input"
              type="text"
              value={String(draft[setting.key] ?? "")}
              onChange={(e) => set(setting.key, e.target.value)}
            />
          )}
          {setting.type === "enum" && (
            <select
              className="panel-input"
              value={String(draft[setting.key] ?? "")}
              onChange={(e) => set(setting.key, e.target.value)}
            >
              {setting.options.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          )}
          {!KNOWN_TYPES.has(setting.type) && (
            // Rendered as text would be a guess, and a guess that
            // saves is worse than one that does not. The same rule
            // the indicator follows for a tone it does not have.
            <span className="meta">
              needs a newer igni — this one does not know the type “
              {setting.type}”
            </span>
          )}
        </label>
      ))}
      <div className="ext-settings-actions">
        <button
          className="btn btn-primary btn-sm"
          disabled={!dirty || busy || broken.length > 0}
          onClick={() => void save()}
        >
          {busy ? "Saving…" : "Save"}
        </button>
        {dirty && !busy && (
          <button className="btn btn-sm" onClick={() => setDraft(saved)}>
            Revert
          </button>
        )}
        {broken.length > 0 && (
          <span className="meta">{broken.join(", ")} needs a number</span>
        )}
        {error && <span className="meta ext-settings-error">{error}</span>}
      </div>
      <div className="meta">
        Applied the next time this extension’s page is opened. A frame already
        up keeps the values it launched with.
      </div>
    </div>
  );
}

/** Installed extensions, their state, and their settings. */
export function ExtensionsPanel({
  client,
  extensions,
  onChanged,
  onClose,
}: {
  client: IgniClient;
  extensions: ExtensionSummary[];
  /** The whole list, as `set_extension_settings` returns it. */
  onChanged: (extensions: ExtensionSummary[]) => void;
  onClose: () => void;
}) {
  return (
    <Drawer title="Extensions" onClose={onClose}>
      {extensions.length === 0 && (
        <div className="msg-info">
          No extensions installed. Add one with <code>igni ext install</code>.
        </div>
      )}
      {extensions.map((extension) => (
        <div className="row ext-row" key={extension.id}>
          <div className="ext-head">
            <div>
              <div className="name">
                {extension.name}{" "}
                <span className="meta">· {extension.version}</span>
              </div>
              <div className="meta">{extension.id}</div>
            </div>
            <span className={stateClass(extension.state)}>
              {extension.state}
            </span>
          </div>
          {extension.reason && <div className="meta">{extension.reason}</div>}
          {extension.settings.length > 0 && (
            <SettingsForm
              extension={extension}
              onSave={async (values) => {
                const result = await client.rpc<{
                  extensions: ExtensionSummary[];
                }>("set_extension_settings", {
                  extension_id: extension.id,
                  values,
                });
                onChanged(result.extensions);
              }}
            />
          )}
        </div>
      ))}
    </Drawer>
  );
}
