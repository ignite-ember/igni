"""`python -m igni_extension.manifest <module>`: the manifest lines the code implies.

Prints, as JSON, the extension's id and `permissions.tools` — every tool
the module's `IgniExtension` registers, namespaced, with its four hints —
so they are written once, in the code:

    python -m igni_extension.manifest acme_tickets_extension
    {"id": "acme.tickets", "tools": [{"name": "acme.tickets.lookup_ticket", ...}]}

`igni-service-bundle` runs it on the extension it bundles. The module is
imported, never run: its `main()` is not called. `module:attribute`
names the extension object when the module holds more than one.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Sequence

from igni_extension.extension import IgniExtension
from igni_extension.hints import ToolHintsMissing


class ManifestLines:
    """Finds a module's extension and says what its manifest needs."""

    @staticmethod
    def extension_in(target: str) -> IgniExtension:
        module_name, _, attribute = target.partition(":")
        module = importlib.import_module(module_name)
        if attribute:
            found = getattr(module, attribute, None)
            if not isinstance(found, IgniExtension):
                raise LookupError(f"{target} is not an IgniExtension")
            return found
        extensions = [value for value in vars(module).values() if isinstance(value, IgniExtension)]
        if len(extensions) != 1:
            raise LookupError(
                f"{module_name} holds {len(extensions)} IgniExtension objects; "
                "name one as module:attribute"
            )
        return extensions[0]

    @classmethod
    def of(cls, target: str) -> dict[str, object]:
        ext = cls.extension_in(target)
        return {"id": ext.id, "tools": ext.manifest_tools()}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m igni_extension.manifest <module>[:attribute]", file=sys.stderr)
        return 2
    try:
        lines = ManifestLines.of(args[0])
    except (ImportError, LookupError, ToolHintsMissing) as exc:
        print(f"igni_extension.manifest: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(lines, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
