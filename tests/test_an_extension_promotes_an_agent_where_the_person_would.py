"""``host.agents.promote_ephemeral`` takes the target ``/agents promote`` does.

``/agents promote <name> --project`` keeps an ephemeral agent in
``.igni/agents/`` (committed, the team's) and ``--personal`` in
``.igni/agents.local/`` (ignored, yours); the panel's RPC carries the same
choice as ``target``. An extension reaching the same action through the host
object model gets the same choice, and leaving it out still means the
project, as it did before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember_code.extensions.host.definition import HOST_SCHEMA
from tests.host_model_helpers import HostRig


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def ok(reply: Any) -> Any:
    assert reply.ok, f"refused: {reply.error}"
    return reply.result


async def test_no_target_promotes_to_the_project(rig: HostRig) -> None:
    ext = rig.install(host=["agents"])
    result = ok(await rig.ask(ext, "call", "agents.promote_ephemeral", name="helper"))
    assert rig.session.promoted == [("helper", "project")]
    assert result["dest"] == ".igni/agents/helper.md"


@pytest.mark.parametrize(
    ("target", "dest"),
    [("project", ".igni/agents/helper.md"), ("personal", ".igni/agents.local/helper.md")],
)
async def test_the_target_is_passed_through(rig: HostRig, target: str, dest: str) -> None:
    ext = rig.install(host=["agents"])
    result = ok(
        await rig.ask(ext, "call", "agents.promote_ephemeral", name="helper", target=target)
    )
    assert rig.session.promoted == [("helper", target)]
    assert result["dest"] == dest


async def test_an_unknown_target_is_invalid_and_moves_nothing(rig: HostRig) -> None:
    ext = rig.install(host=["agents"])
    reply = await rig.ask(ext, "call", "agents.promote_ephemeral", name="helper", target="team")
    assert not reply.ok
    assert reply.error.code == "invalid"
    assert "project" in reply.error.message and "personal" in reply.error.message
    assert rig.session.promoted == []


def test_the_schema_declares_the_optional_target() -> None:
    [agents] = [o for o in HOST_SCHEMA.objects if o.path == "agents"]
    [promote] = [m for m in agents.methods if m.name == "promote_ephemeral"]
    [_name, target] = promote.params
    assert (target.name, target.type, target.optional) == ("target", "str", True)
    assert "personal" in target.doc and ".igni/agents.local/" in target.doc
