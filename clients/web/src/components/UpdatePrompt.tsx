import { useState } from "react";

export interface UpdatePromptInfo {
  available: boolean;
  current_version: string;
  latest_version: string;
  download_url?: string;
}

interface TauriInvoke {
  (cmd: string): Promise<unknown>;
}

function tauriInvoke(): TauriInvoke | undefined {
  return (window as unknown as {
    __TAURI__?: { core?: { invoke?: TauriInvoke } };
  }).__TAURI__?.core?.invoke;
}

export function UpdatePrompt({
  info,
  onDismiss,
}: {
  info: UpdatePromptInfo;
  onDismiss: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const invoke = tauriInvoke();

  const onInstall = async () => {
    if (!invoke) {
      if (info.download_url) window.open(info.download_url, "_blank");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await invoke("ember_install_update");
    } catch (e) {
      setBusy(false);
      setError(String(e));
    }
  };

  return (
    <div
      className="update-prompt-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onDismiss();
      }}
    >
      <div className="update-prompt" role="dialog" aria-modal="true">
        <div className="update-prompt-title">Update available</div>
        <div className="update-prompt-body">
          igni <strong>{info.latest_version}</strong> is ready to
          install. You're currently on{" "}
          <strong>{info.current_version}</strong>.
        </div>
        {error && <div className="update-prompt-error">{error}</div>}
        <div className="update-prompt-actions">
          <button
            type="button"
            className="btn"
            onClick={onDismiss}
            disabled={busy}
          >
            Later
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={onInstall}
            disabled={busy}
          >
            {busy ? "Installing…" : invoke ? "Install & restart" : "Download"}
          </button>
        </div>
      </div>
    </div>
  );
}
