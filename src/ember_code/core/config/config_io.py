"""Low-level config I/O primitives — ``DictMerger`` and ``YamlSource``.

Leaf module with two small value classes shared by
:class:`SettingsLoader` and :class:`ManagedPolicySource`. Neither
class depends on :class:`Settings`, :class:`SettingsLoader`, or
:class:`ManagedPolicySource` — this module sits BELOW everything
else in :mod:`core.config` so any of them can import it freely
without cycles.

Previously these primitives lived as staticmethods on
:class:`SettingsLoader` (``deep_merge`` / ``load_yaml``) plus a
duplicated ``_load_yaml`` on :class:`ManagedPolicySource` with an
apologetic docstring blaming "circular import." Promoting them to
their own module dissolves the excuse.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class DictMerger:
    """Deep-merge two nested dicts, ``override`` wins.

    Kept as a class (rather than a bare free function) so a future
    variant — list-append instead of list-replace, say — has an
    obvious home: subclass and override :meth:`merge`. The default
    behaviour matches YAML precedence: non-dict values replace
    outright, dict values recurse.

    The bulk of callers reach for :meth:`DictMerger.deep` (the
    staticmethod form) because they don't need a stateful merger.
    """

    @staticmethod
    def deep(base: dict, override: dict) -> dict:
        """Deep merge ``override`` into ``base``, returning a new
        dict. Non-dict values on either side replace rather than
        merge — matches YAML precedence for lists.
        """
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = DictMerger.deep(result[key], value)
            else:
                result[key] = value
        return result

    def merge(self, base: dict, override: dict) -> dict:
        """Instance form of :meth:`deep` — subclasses override this
        to customise the merge policy (e.g. list-append semantics)
        while keeping the staticmethod entrypoint for the common
        case."""
        return self.deep(base, override)


class YamlSource:
    """One YAML file on disk projected as a merge-ready dict.

    Encapsulates the file-existence-check + ``yaml.safe_load`` +
    dict-guard idiom that used to be duplicated across
    :class:`SettingsLoader` and :class:`ManagedPolicySource`. Callers
    that just need the dict use :meth:`load`; the ``exists`` property
    is available for callers that want to distinguish "file missing"
    from "file present but empty".
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.exists()

    def load(self) -> dict:
        """Load the YAML file, returning ``{}`` when the file
        doesn't exist or the parsed payload isn't a dict.
        """
        if not self._path.exists():
            return {}
        with open(self._path) as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
