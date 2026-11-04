"""What one extension may reach of the host model.

``permissions.host`` in the manifest names the objects an extension
uses; an administrator approves the list. An object that is not granted
does not exist for the extension: reads fail, methods are refused, and
its signals never arrive (``docs/HOST_OBJECT_MODEL.md``, *Granted per
object*).

Two grants are not ``permissions.host`` entries, because the manifest
already says them:

* ``workspace`` is ``permissions.workspace`` — ``read`` or ``write`` —
  limited to ``workspacePaths`` exactly as it is for everything else
  the host tells an extension about the project;
* ``None`` — an extension's own state, its own settings, its own
  notifications — is always granted.

The grammar of a ``permissions.host`` entry is the one ``permissions.rpc``
uses: an exact name, or a ``*``-suffixed prefix (``chat.*``).
"""

from __future__ import annotations

from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.permissions import WorkspaceAccess

#: The grant that ``permissions.workspace`` decides.
WORKSPACE = "workspace"


class HostGrants:
    """One extension's grants, read from its manifest."""

    def __init__(self, manifest: ExtensionManifest) -> None:
        self.manifest = manifest
        self.extension_id = manifest.id
        self.workspace = WorkspaceAccess(manifest)
        self._host = tuple(manifest.permissions.host)

    def allows(self, permission: str | None) -> bool:
        """Whether a member needing ``permission`` exists for this extension."""
        if permission is None:
            return True
        if permission == WORKSPACE:
            return self.workspace.granted
        for grant in self._host:
            if grant.endswith("*"):
                if permission.startswith(grant[:-1]):
                    return True
            elif grant == permission:
                return True
        return False

    def granted(self, known: set[str]) -> list[str]:
        """Every known permission this extension holds, for its hello."""
        return sorted(p for p in known if self.allows(p))

    @staticmethod
    def unknown(grants: list[str], known: set[str]) -> list[str]:
        """Entries that match no permission the model has.

        Noted at install, not refused: a manifest asking for something
        newer than this igni is usually early, not wrong, and refusing
        it would make every new member a breaking change for everybody
        who guessed early.
        """
        stray: list[str] = []
        for grant in grants:
            if grant.endswith("*"):
                prefix = grant[:-1]
                if not any(p.startswith(prefix) for p in known):
                    stray.append(grant)
            elif grant not in known or grant == WORKSPACE:
                stray.append(grant)
        return stray


__all__ = ["WORKSPACE", "HostGrants"]
