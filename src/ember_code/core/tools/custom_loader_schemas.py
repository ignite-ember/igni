"""Pydantic wire/data models for the custom-tool discovery run.

Extracted from :mod:`custom_loader` per the sibling schemas
convention (mirrors :mod:`shell_orphan_schemas` and
:mod:`process_store_schemas` in this exact directory). Every wire /
result model this subsystem hands across a module boundary lives
here so Rule 1 stays discoverable at one path.

Consumers:

* :class:`ToolSource` — a single ``(name_prefix, tools_dir)`` scan
  target. Collapses the duplicated user-dir vs plugin-dir loops in
  :class:`CustomToolLoader` into a single uniform iteration.
  ``name_prefix`` is ``"custom"`` for user dirs and
  ``"custom_<plugin>"`` for plugin dirs — the loader appends
  ``_<file.stem>`` so a plugin's tool file can never shadow or be
  shadowed by a same-named file in the user's own ``.ember/tools/``.
* :class:`LoadedFile` — one successfully loaded ``.py`` file, its
  emitted toolkit name, and the count of ``@tool``-decorated
  functions it contributed. Additive to the existing
  ``logger.info(...)`` per-file messages — the model does not
  replace grep-based debugging, it complements it.
* :class:`SkippedFile` — a ``.py`` file the loader deliberately
  passed over. ``reason`` is a Literal so UIs / registry can render
  each branch distinctly instead of parsing free-form strings.
* :class:`FailedFile` — an import failure surfaced (not swallowed
  at ``logger.warning``) so the caller — registry, plugin-reload,
  or the UI — can display the error to the user instead of
  requiring them to tail the log.
* :class:`DiscoveryResult` — the structured return of
  :meth:`CustomToolLoader.discover`. Holds ``list[Toolkit]``
  (agno's :class:`Toolkit`, not a Pydantic type) so
  ``arbitrary_types_allowed=True`` is required.

The back-compat :func:`ember_code.core.tools.custom_loader.load_custom_tools`
shim returns ``result.toolkits`` — a plain ``list[Toolkit]`` — so
existing callers (:mod:`registry`, :mod:`tools_builder`) keep
working unchanged. New callers migrate incrementally to
:meth:`CustomToolLoader.discover` for the full result shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from agno.tools import Toolkit
from pydantic import BaseModel, ConfigDict, Field


class ToolSource(BaseModel):
    """A single scan target for :meth:`CustomToolLoader.discover`.

    Collapses the two duplicated loops (user dirs vs plugin dirs)
    in the previous procedural implementation into a uniform
    iteration over a list of these. The loader emits toolkit names
    of the form ``<name_prefix>_<file.stem>`` — so ``name_prefix``
    is ``"custom"`` for user dirs and ``"custom_<plugin>"`` for
    plugin dirs. An accidental rename here would break any agent
    that references a plugin toolkit by name.
    """

    name_prefix: str
    tools_dir: Path


class LoadedFile(BaseModel):
    """One successfully loaded custom-tool file.

    Additive to the ``logger.info(...)`` per-file message the
    loader still emits — the model records the same data in a
    structured shape for UIs / registry consumers, and the log
    line stays so grep-based debugging keeps working.
    """

    path: Path
    toolkit_name: str
    function_count: int


class SkippedFile(BaseModel):
    """A ``.py`` file the loader deliberately passed over.

    ``reason`` is a :class:`typing.Literal` so downstream code can
    switch on it exhaustively (mypy-checked) rather than
    string-matching a free-form message. The three current
    branches map 1:1 to the loader's skip paths:

    * ``underscore_prefix`` — filename starts with ``_``.
    * ``no_functions`` — file imported cleanly but contributed
      zero ``@tool``-decorated :class:`Function` instances.
    * ``not_a_directory`` — the scan target itself doesn't exist
      or isn't a directory (a whole :class:`ToolSource` skipped,
      not a single file — ``path`` is the missing dir in that
      case).
    """

    path: Path
    reason: Literal["underscore_prefix", "no_functions", "not_a_directory"]


class FailedFile(BaseModel):
    """A ``.py`` file whose import raised.

    Previously these were ``logger.warning``'d and discarded — the
    registry / UI had no way to surface them to the user. Now the
    error is captured on the result so callers can render a
    "these custom tools failed to load: ..." message without
    tailing the log. ``error_type`` is the exception class name
    (``ImportError``, ``SyntaxError``, ...) so UIs can group by
    cause; ``error`` is ``str(exc)`` for the human-readable
    message.
    """

    path: Path
    error: str
    error_type: str


class DiscoveryResult(BaseModel):
    """Structured return of :meth:`CustomToolLoader.discover`.

    Holds ``list[Toolkit]`` where :class:`Toolkit` is agno's
    class, not a Pydantic type — so
    ``arbitrary_types_allowed=True`` is required. Matches the
    existing pattern in the ``tools/`` package where result models
    hold agno objects.

    The back-compat :func:`load_custom_tools` shim returns only
    :attr:`toolkits` (a plain ``list[Toolkit]``) so callers that
    iterate the result under ``list(...)`` don't silently get the
    model's fields instead of the toolkits. New callers migrate
    to :meth:`CustomToolLoader.discover` to get ``loaded`` /
    ``skipped`` / ``failed`` alongside the toolkits.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    toolkits: list[Toolkit] = Field(default_factory=list)
    loaded: list[LoadedFile] = Field(default_factory=list)
    skipped: list[SkippedFile] = Field(default_factory=list)
    failed: list[FailedFile] = Field(default_factory=list)
