"""The safety stages look up arguments that the tools actually take.

Both guard bypasses this file exists to prevent were the same bug, and neither
was a logic error — the logic was fine. Each stage asked a tool call for an
argument under a key the tool does not have, got nothing, and concluded there
was nothing to block:

* ``BlockedCommandStage`` read ``args["args"]``, correct for agno's
  ``ShellTools.run_shell_command(args: list[str])``. ``EmberShellTools``
  replaced it with ``run_shell_command(command: str)``. The deny list —
  ``rm -rf /``, the fork bomb — had never blocked anything.

* ``ProtectedPathStage`` read ``args["file_path"]`` for every write tool.
  ``edit_file``, ``edit_file_replace_all`` and ``create_file`` do take
  ``file_path``; agno's ``save_file`` takes ``file_name``. Writes to ``.env``,
  ``*.pem``, ``*.key`` and ``credentials.*`` went through unguarded. Three of
  four paths working is why nobody noticed.

Thirty-eight tests passed throughout, because they all passed the *legacy*
argument shape — ``{"args": [...]}``, and ``{"file_path": ".env"}`` for
``save_file``. Example-based tests cannot catch this: they assert the stage
blocks what they hand it, and they hand it the shape the stage expects.

So this checks the join instead of the behaviour. For every function a stage
claims to gate, it resolves the real callable through ``ToolSpecCatalog``,
reads its signature, and asserts the stage has at least one lookup key the
function will actually be called with. Swap a toolkit again and this fails on
the signature, at the seam, rather than silently in production.
"""

from __future__ import annotations

import inspect

import pytest

from ember_code.core.hooks.permission_pipeline import (
    PATH_ARG_KEYS,
    SHELL_ARG_KEYS,
    BlockedCommandStage,
    ProtectedPathStage,
)
from ember_code.core.tools.tool_spec import ToolSpecCatalog

#: Imported, never restated. Writing the keys out here would make this file
#: agree with itself rather than with the stage — which is the shape of the bug
#: it exists to prevent.
_PROTECTED_PATH_KEYS = PATH_ARG_KEYS
_SHELL_KEYS = SHELL_ARG_KEYS


def _function_signatures() -> dict[str, list[str]]:
    """Every LLM-facing function name mapped to its real parameter names."""
    catalog = ToolSpecCatalog.default()
    specs = getattr(catalog, "specs", None)
    if specs is None:  # pragma: no cover — shape guard, not a branch we want
        specs = list(getattr(catalog, "_specs", {}).values())
    out: dict[str, list[str]] = {}
    for spec in specs:
        for name in spec.agno_function_names:
            func = getattr(spec.toolkit_cls, name, None)
            if func is None:
                continue
            # Drop ``self``: a tool call supplies arguments, not the receiver.
            out[name] = list(inspect.signature(func).parameters)[1:]
    return out


@pytest.fixture(scope="module")
def signatures() -> dict[str, list[str]]:
    sigs = _function_signatures()
    # A resolver that silently returned nothing would make every assertion
    # below vacuous, which is the failure mode this whole file is about.
    assert len(sigs) > 10, sigs
    return sigs


class TestEveryGatedFunctionIsReachable:
    """A stage that gates a name it cannot resolve gates nothing."""

    @pytest.mark.parametrize("name", sorted(ProtectedPathStage.WRITE_TOOL_FUNCTIONS))
    def test_the_write_tool_exists(self, name, signatures):
        assert name in signatures, (
            f"ProtectedPathStage gates {name!r}, which resolves to no toolkit function. "
            "Either it was renamed and the set was not updated, or the set names "
            "something that never existed — both mean the guard does not run."
        )

    @pytest.mark.parametrize("name", sorted(BlockedCommandStage.SHELL_TOOL_FUNCTIONS))
    def test_the_shell_tool_exists(self, name, signatures):
        assert name in signatures, (
            f"BlockedCommandStage gates {name!r}, which resolves to no toolkit function."
        )


class TestEveryGatedFunctionCanBeRead:
    """The bug itself: the name resolves, the argument does not."""

    @pytest.mark.parametrize("name", sorted(ProtectedPathStage.WRITE_TOOL_FUNCTIONS))
    def test_a_write_tool_has_a_path_key_the_stage_looks_under(self, name, signatures):
        params = signatures.get(name, [])
        overlap = set(params) & set(_PROTECTED_PATH_KEYS)

        assert overlap, (
            f"{name}{tuple(params)} has no parameter among {_PROTECTED_PATH_KEYS}, so "
            f"ProtectedPathStage reads '' for it and returns no_block — a protected "
            f"path written through {name} would go through unguarded. This is exactly "
            "how save_file(file_name=...) bypassed the guard while the other three "
            "write tools worked."
        )

    @pytest.mark.parametrize("name", sorted(BlockedCommandStage.SHELL_TOOL_FUNCTIONS))
    def test_a_shell_tool_has_a_command_key_the_stage_looks_under(self, name, signatures):
        params = signatures.get(name, [])
        overlap = set(params) & set(_SHELL_KEYS)

        assert overlap, (
            f"{name}{tuple(params)} has no parameter among {_SHELL_KEYS}, so "
            f"BlockedCommandStage joins '' and matches no blocked pattern — the deny "
            "list would be inert, which is the state this repository shipped in."
        )


class TestTheStagesActuallyBlockTheProductionShape:
    """The join is necessary, not sufficient: assert the block too.

    Parametrised over both shapes on purpose. The legacy form is what the old
    tests used and it kept passing while production was unguarded, so checking
    only the new one would repeat the mistake in the opposite direction.
    """

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param({"command": "rm -rf /"}, id="production"),
            pytest.param({"args": ["rm", "-rf", "/"]}, id="legacy-list"),
            pytest.param({"args": "rm -rf /"}, id="legacy-string"),
        ],
    )
    def test_the_deny_list_fires(self, args):
        stage = BlockedCommandStage(blocked_commands=["rm -rf /"])
        ctx = _ctx("run_shell_command", args)

        assert stage.check_sync(ctx).blocked, args

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param({"contents": "x", "file_name": ".env"}, id="save_file"),
            pytest.param({"file_path": ".env", "content": "x"}, id="create_file"),
        ],
    )
    def test_a_protected_path_is_blocked(self, args):
        stage = ProtectedPathStage(protected_paths=[".env"])
        name = "save_file" if "file_name" in args else "create_file"
        ctx = _ctx(name, args)

        assert stage.check_sync(ctx).blocked, args


def _ctx(name: str, args: dict):
    """A ToolCallContext carrying exactly what the wire carries.

    ``ctx.args`` is the model's raw argument dict, passed through unchanged:
    agno builds it in ``FunctionCall._build_hook_args`` and ``ToolCallInvoker``
    then calls ``func(**ctx.args)``. Nothing renames anything on the way, which
    is why the key a stage reads has to be a key the function declares.
    """
    from ember_code.core.hooks.tool_hook import ToolCallContext

    return ToolCallContext(name=name, args=args, func=lambda **_: None)
