"""The injected-discovery Protocol described a function that does not exist.

``MarkdownCommandDispatcher`` takes its discovery dependency by
constructor injection and types it with the
:class:`DiscoverMarkdownCommands` Protocol. A Protocol is the *only*
description of that dependency a type-checker ever sees, so anything it
leaves out is a call nobody verifies.

It left out ``group_dir``. ``try_render`` has always passed it, the real
:func:`discover_markdown_commands` has always accepted it, and the
Protocol in between said the parameter did not exist — the one artefact
whose entire job was to be checked was the one that was wrong (SWU-100).

Two things kept it invisible, and this file pins both:

* the Protocol drifting from the function it stands for, and
* ``call-arg`` sitting in the project-wide ``disable_error_code`` list,
  which is precisely the code mypy reports for an unexpected keyword
  argument. With it off, ``mypy`` was green on a call it could see was
  wrong.

Signatures are compared by introspection rather than by a mypy
subprocess: a full ``mypy`` run over this module takes ~17 s, which is
not a unit test. The type-level guarantee is the per-module
``enable_error_code`` override, and ``test_call_arg_is_enabled_for_this_
module`` is what stops that override from being deleted silently.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import tomllib
from pathlib import Path

from ember_code.backend.markdown_command_dispatcher import (
    DiscoverMarkdownCommands,
    MarkdownCommandDispatcher,
)
from ember_code.core.utils.markdown_commands import discover_markdown_commands

_ROOT = Path(__file__).resolve().parent.parent
_MODULE = "ember_code.backend.markdown_command_dispatcher"


def _params(sig: inspect.Signature) -> list[tuple[str, inspect._ParameterKind, object, object]]:
    return [(p.name, p.kind, p.default, p.annotation) for p in sig.parameters.values()]


class TestTheProtocolDescribesTheRealFunction:
    def test_every_parameter_matches(self):
        """Name, kind, default and annotation — not just the names. A
        keyword-only ``read_claude`` in the Protocol against a
        positional-or-keyword one in the function is the same class of lie,
        one step subtler."""
        protocol = inspect.signature(DiscoverMarkdownCommands.__call__)
        real = inspect.signature(discover_markdown_commands)

        assert _params(protocol)[1:] == _params(real), (
            "DiscoverMarkdownCommands.__call__ no longer describes discover_markdown_commands"
        )

    def test_the_return_annotation_matches(self):
        protocol = inspect.signature(DiscoverMarkdownCommands.__call__)
        real = inspect.signature(discover_markdown_commands)

        assert protocol.return_annotation == real.return_annotation

    def test_group_dir_is_there_at_all(self):
        """The specific parameter the Protocol forgot. Spelled out so a
        regression names itself instead of arriving as a diff of two
        parameter lists."""
        protocol = inspect.signature(DiscoverMarkdownCommands.__call__)

        assert "group_dir" in protocol.parameters


class TestTheCallSiteFitsTheProtocol:
    """The half that a signature comparison alone does not cover: the
    Protocol and the function can agree with each other and both disagree
    with what ``try_render`` actually passes."""

    def _discovery_call(self) -> ast.Call:
        source = inspect.getsource(MarkdownCommandDispatcher.try_render)
        tree = ast.parse(textwrap.dedent(source))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_discover"
        ]

        assert len(calls) == 1, f"expected one self._discover(...) call, found {len(calls)}"
        return calls[0]

    def test_the_arguments_bind_against_the_protocol(self):
        """``bind`` raises ``TypeError`` for exactly the mistake that was
        made: a keyword the callable does not declare."""
        call = self._discovery_call()
        sig = inspect.signature(DiscoverMarkdownCommands.__call__)
        positional = [inspect.Parameter.empty] * (len(call.args) + 1)  # +1 for ``self``
        keywords = {kw.arg: inspect.Parameter.empty for kw in call.keywords if kw.arg}

        sig.bind(*positional, **keywords)

    def test_it_still_passes_group_dir(self):
        """If the call site stops passing it, the Protocol should shed it
        too — this test failing is the prompt to do that, not to widen the
        Protocol and forget."""
        call = self._discovery_call()

        assert "group_dir" in {kw.arg for kw in call.keywords}


class TestMypyCanStillSeeIt:
    def test_call_arg_is_enabled_for_this_module(self):
        """Without this override the Protocol is decorative: ``call-arg`` is
        disabled project-wide, so mypy is told not to report the unexpected
        keyword argument that is the whole finding.

        Scoped to this module rather than removed from the global list
        because dropping it globally reports 7 errors across 5 files, 6 of
        them unrelated to this one.
        """
        config = tomllib.loads((_ROOT / "pyproject.toml").read_text())

        assert "call-arg" in config["tool"]["mypy"]["disable_error_code"], (
            "call-arg is no longer disabled globally — the per-module "
            "override below is now redundant and should be removed"
        )
        overrides = [
            o for o in config["tool"]["mypy"]["overrides"] if _MODULE in o.get("module", [])
        ]

        assert overrides, f"no mypy override re-enables call-arg for {_MODULE}"
        assert any("call-arg" in o.get("enable_error_code", []) for o in overrides)
