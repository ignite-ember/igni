"""Tool-category catalog + Bash mutation heuristic.

Absorbs the three module-level frozensets (``FILE_EDIT_TOOLS`` /
``FILE_READ_TOOLS`` / ``SHELL_TOOLS``) and the ``_bash_command_mutates``
free function into two cohesive classes:

* :class:`ToolCategoryCatalog` — Pydantic model owning the tool
  category sets and exposing ``.is_edit`` / ``.is_read`` / ``.is_shell``
  predicates. The mode strategies read categories through the catalog
  so the raw ``in`` checks disappear from the pipeline.
* :class:`BashCommand` — value object wrapping the Bash mutation
  regex; ``.mutates()`` returns True when the command matches any
  filesystem-writing verb / redirect. Owns the compiled regex as a
  ``ClassVar`` so it isn't a bare module-level constant.

The module still re-exports the three frozensets under their original
names because
:mod:`ember_code.core.config.permission_eval.__init__` re-exports them
for callers (tests import ``FILE_EDIT_TOOLS`` at module scope).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from pydantic import BaseModel, Field

# ── Tool sets — kept as module-level frozensets so the package
#    ``__init__`` can re-export them for legacy imports. The
#    ``ToolCategoryCatalog`` default_factory seeds itself from these.


#: Tools that mutate the filesystem — used by ``acceptEdits``
#: (auto-approve) and ``plan`` (block). Names match Claude Code's
#: Edit/Write/NotebookEdit set, plus ember-code's ``edit_file_*`` /
#: ``save_file`` / ``create_file`` variants. Bash is handled by
#: :class:`BashCommand` because shell commands may or may not mutate.
FILE_EDIT_TOOLS: frozenset[str] = frozenset(
    {
        "Edit",
        "Write",
        "NotebookEdit",
        "save_file",
        "edit_file",
        "edit_file_replace_all",
        "create_file",
    }
)

#: Tools that only read — auto-allowed in plan mode so the agent can
#: investigate without prompting. Catalog names + internal Agno
#: function names (the evaluator may see either depending on the
#: call site).
#
# ``Read`` / ``Grep`` / ``Glob`` / ``LS`` and their function names were
# here until those toolkits left the registry. An auto-allow for a
# tool that cannot be attached grants nothing and reads as though it
# does. Reading and searching in plan mode go through ``Bash``, which
# is *not* auto-allowed — plan mode blocks mutating shell and prompts
# for the rest, which is the intended posture.
FILE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "WebSearch",
        "duckduckgo_search",
        "duckduckgo_news",
        "WebFetch",
        "fetch_url",
        "fetch_json",
        "CodeIndex",
        # codeindex_cypher is read-only by hard contract — the toolkit
        # rejects any mutating Cypher before it reaches the driver
        # (cypher_guard.assert_read_only_cypher), so it belongs in the
        # plan-mode auto-allow set. This used to list `codeindex_query` and
        # `codeindex_tree` instead: those are internal service methods that
        # are never registered as agent-facing tools (see
        # tools/codeindex/tool.py:73-76), so the evaluator could not see
        # them, while the ONE tool that does reach it was missing — meaning
        # CodeIndex research in plan mode was not actually auto-allowed.
        "codeindex_cypher",
    }
)

#: Shell tools — checked specially in plan mode via
#: :class:`BashCommand.mutates`.
SHELL_TOOLS: frozenset[str] = frozenset({"Bash", "run_shell_command"})


class BashCommand:
    """Value object wrapping a shell command string.

    Owns the mutation heuristic that used to be the free
    ``_bash_command_mutates`` function. Constructing from raw args
    is done via :meth:`from_args` so the multi-shape input handling
    (``command`` string vs ``command`` list vs ``args`` list vs
    ``args`` string) lives on the class, not scattered through the
    pipeline.
    """

    #: Mutation markers in a shell command string. Plan mode denies
    #: any Bash call whose command matches one of these. Conservative
    #: by design: false positives just route to the user prompt,
    #: false negatives let an edit slip through plan mode (worse).
    #:
    #: Covered: in-place edits (``sed -i``, ``perl -i``), redirects to
    #: a file (``>``, ``>>``), and obvious filesystem-mutating verbs
    #: as the first token after ``;`` / ``&&`` / ``|`` / start-of-
    #: command. Stderr merges like ``2>&1`` don't trigger because the
    #: regex requires a non-``&`` character after ``>``.
    _MUTATION_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:^|[\s|;&(`])"  # start-of-command or shell separator
        r"(?:rm|rmdir|mv|cp|mkdir|touch|chmod|chown|ln|dd|truncate|tee)\b"
        r"|\bsed\s+-i\b"
        r"|\bperl\s+-i\b"
        r"|>\s*[^&\s|]"  # write redirect to a path (not >& or > |)
        r"|>>"
    )

    __slots__ = ("_command",)

    def __init__(self, command: str) -> None:
        self._command = command

    @property
    def command(self) -> str:
        return self._command

    @classmethod
    def from_args(cls, tool_args: dict[str, Any]) -> BashCommand:
        """Build a BashCommand from a tool_args dict, absorbing all
        the shape combinations legacy shell tools use:

        * ``{"command": "rm -rf x"}`` (Claude Code style)
        * ``{"command": ["rm", "-rf", "x"]}`` (some Agno variants)
        * ``{"args": ["rm", "-rf", "x"]}`` (legacy ShellTools)
        * ``{"args": "rm -rf x"}`` (very legacy)
        """
        cmd_str = ""
        raw = tool_args.get("command")
        if isinstance(raw, str):
            cmd_str = raw
        elif isinstance(raw, list):
            cmd_str = " ".join(str(p) for p in raw)
        else:
            args = tool_args.get("args")
            if isinstance(args, list):
                cmd_str = " ".join(str(p) for p in args)
            elif isinstance(args, str):
                cmd_str = args
        return cls(cmd_str)

    def mutates(self) -> bool:
        """Heuristic: does this Bash invocation write to the
        filesystem? Returns ``True`` for any match of the mutation
        regex."""
        if not self._command:
            return False
        return bool(self._MUTATION_RE.search(self._command))


class ToolCategoryCatalog(BaseModel):
    """Category classifier for tool names.

    Encapsulates the three tool sets so the mode strategies use
    ``catalog.is_edit(name)`` instead of ``name in FILE_EDIT_TOOLS``.
    Alternative catalogs (with extra tools) can be built for tests
    without monkey-patching module globals.
    """

    file_edit_tools: frozenset[str] = Field(default_factory=lambda: FILE_EDIT_TOOLS)
    file_read_tools: frozenset[str] = Field(default_factory=lambda: FILE_READ_TOOLS)
    shell_tools: frozenset[str] = Field(default_factory=lambda: SHELL_TOOLS)

    def is_edit(self, tool_name: str) -> bool:
        """Is ``tool_name`` a filesystem-mutating file tool?"""
        return tool_name in self.file_edit_tools

    def is_read(self, tool_name: str) -> bool:
        """Is ``tool_name`` a read-only file/search/web tool?"""
        return tool_name in self.file_read_tools

    def is_shell(self, tool_name: str) -> bool:
        """Is ``tool_name`` a shell-execution tool (Bash-like)?"""
        return tool_name in self.shell_tools

    def bash_mutates(self, tool_args: dict[str, Any]) -> bool:
        """Convenience: build a :class:`BashCommand` and ask if it
        mutates. Kept on the catalog for locality — the mode
        strategies already have a catalog reference and shouldn't
        need to know about the ``BashCommand`` type directly."""
        return BashCommand.from_args(tool_args).mutates()

    @classmethod
    def default(cls) -> ToolCategoryCatalog:
        """Return the shared default catalog. Instances are cheap;
        this is mainly a marker for "the vanilla category set" so
        the pipeline's default_factory has a single call site."""
        return cls()
