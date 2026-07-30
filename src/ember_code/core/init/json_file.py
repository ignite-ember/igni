"""Fail-soft JSON file value object for the init subsystem."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class JsonFile(BaseModel):
    """A named JSON file location with fail-soft read/write semantics.

    Immutable value object — a :class:`JsonFile` is a name for a
    location, not a mutable buffer. Owns the two invariants shared
    by every JSON file this subsystem touches:

    * **Load** — missing / unparseable file → empty dict.
    * **Save** — mkdir parents, indent=2, trailing newline.

    The typed :meth:`load_model` / :meth:`save_model` pair closes the
    raw-dict seam at Pydantic-model callers (Rule 1) so callsites
    don't repeat the ``load → model_validate`` or ``model_dump →
    save`` dance.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    path: Path

    # ── Raw dict I/O ──────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        """Load the file as a dict. Missing / invalid → ``{}``.

        Fail-soft: any read/parse error resolves to an empty dict so
        callers can treat "no file" and "corrupted file" uniformly.
        """
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        """Write ``data`` as pretty-printed JSON with a trailing newline.

        Not fail-soft: write errors (permission denied, disk full)
        propagate so callers see them — losing state on save is worse
        than raising.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    # ── Typed model I/O ───────────────────────────────────────────

    def load_model(self, model_cls: type[T]) -> T:
        """Load and validate into ``model_cls``. Fail-soft.

        Missing / unparseable JSON → ``model_cls()`` (via :meth:`load`
        returning ``{}``). Pydantic validation failure → also
        ``model_cls()``, with a ``logger.warning`` that names the
        offending path so the pre-existing diagnostic is preserved.
        """
        raw = self.load()
        try:
            return model_cls.model_validate(raw)
        except ValidationError:
            logger.warning(
                "Ignoring unparseable %s — starting from empty settings.",
                self.path,
            )
            return model_cls()

    def save_model(self, model: BaseModel, **dump_kwargs: object) -> None:
        """Serialise ``model`` and persist via :meth:`save`.

        ``dump_kwargs`` (``exclude_none`` / ``exclude_defaults`` /
        ``by_alias`` / …) are forwarded to
        :meth:`~pydantic.BaseModel.model_dump` so the callsite keeps
        control of the on-disk shape — this class stays model-agnostic.
        """
        payload = model.model_dump(**dump_kwargs)
        self.save(payload)
