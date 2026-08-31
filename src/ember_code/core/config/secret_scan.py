"""Is there a credential in a file meant to be shared?

The project's config is on its way to being committed, and it is the
same file that carries provider definitions — which model ids exist,
their URLs, their context windows. Those are worth sharing. The keys
that reach them are not.

The existing signal for "do not share this" is the ``.local.`` filename
suffix, and it works only because the whole config directory is
gitignored. The moment the directory is committed, one wrong ignore line
publishes a provider key — so the boundary moves from a filename to a
*directory*: ``~/.igni`` holds credentials, the project holds what a
team shares, and there is no path from the first into a repository.

Nothing new is needed to express that. The config tiers already
deep-merge per field, so a home config carrying nothing but

    models:
      registry:
        MiniMax-M3:
          api_key: sk-...

combines with a project config carrying the provider, model id, URL and
context window. And ``api_key_env`` / ``api_key_cmd`` were always the
shareable way to reach a secret: they *name* one rather than containing
it.

What is missing is anybody saying so. This finds literal keys in files
that are meant to be shared, and says which line and what to do about
it. A warning rather than a refusal: a deployment mid-migration should
keep working, and the person who needs to move a key is often not the
person starting the session.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from ember_code.core.paths import CONFIG_DIR

logger = logging.getLogger(__name__)

#: Entry fields that hold a credential rather than naming one.
#:
#: ``api_key_env`` and ``api_key_cmd`` are deliberately absent: an
#: environment-variable name and a shell command are safe to commit,
#: which is what makes them the answer to this warning rather than more
#: of the problem.
SECRET_FIELDS = ("api_key",)

#: The sentinel meaning "use the signed-in cloud token", which is not a
#: secret — it is an instruction to go and find one.
_NOT_A_SECRET = ("cloud_token",)


def _secret_models(payload: Any) -> list[str]:
    """Registry entry names whose config embeds a literal key."""
    if not isinstance(payload, dict):
        return []
    registry = (payload.get("models") or {}).get("registry")
    if not isinstance(registry, dict):
        return []

    found: list[str] = []
    for name, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        for field in SECRET_FIELDS:
            value = entry.get(field)
            if isinstance(value, str) and value and value not in _NOT_A_SECRET:
                found.append(str(name))
                break
    return found


def scan_project_config(project_dir: Path) -> list[str]:
    """Warnings about credentials in the project's shareable config.

    Only ``config.yaml`` — the one a team shares. ``config.local.yaml``
    is a person's own and gitignored, so a key there is a choice rather
    than a mistake, though it will still have to move when the config
    directory is committed.
    """
    path = Path(project_dir) / CONFIG_DIR / "config.yaml"
    if not path.is_file():
        return []

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("Could not scan %s for credentials: %s", path, exc)
        return []

    names = _secret_models(payload)
    if not names:
        return []

    listed = ", ".join(repr(n) for n in sorted(names))
    subject = "a literal API key" if len(names) == 1 else "literal API keys"
    return [
        f"{path.name} carries {subject} for {listed}. That file is meant to be "
        f"shared, so the key would be shared with it. Move the `api_key` line to "
        f"~/{CONFIG_DIR}/config.yaml — the two are merged field by field, so the "
        f"definition can stay here — or replace it with `api_key_env`, which names "
        f"the secret instead of holding it."
    ]
