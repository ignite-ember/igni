"""The host object model is complete by construction.

``docs/HOST_OBJECT_MODEL.md``, *Rules*: every RPC method, every action
the UI sends and every event the host broadcasts is a member of the
model, or is listed under *Not reachable* with its reason. A feature
added to igni without its member fails here, so the model cannot fall
behind the app. The mapping is :mod:`ember_code.extensions.host.coverage`.

Also here: every built member is implemented and every stage-2 member is
not — a member is either answered or answered as not yet available,
never silently missing.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from ember_code.extensions.events import HostTopic
from ember_code.extensions.host import coverage
from ember_code.extensions.host.definition import HOST_SCHEMA
from ember_code.extensions.host.members import ALL_MEMBERS
from ember_code.extensions.host.spec import MethodSpec, PropertySpec, SignalSpec
from ember_code.protocol.rpc import RpcMethod
from ember_code.protocol.schemas import be_events, fe_actions, mirroring
from ember_code.protocol.schemas.enums import PushChannel
from ember_code.protocol.schemas.envelope import Message
from tests.host_model_helpers import HostRig

DOC = Path(__file__).resolve().parent.parent / "docs" / "HOST_OBJECT_MODEL.md"


def member_exists(path: str) -> bool:
    try:
        HOST_SCHEMA.resolve(path)
    except KeyError:
        return False
    return True


def wire_types(module: object) -> dict[str, str]:
    """``{wire type: class name}`` for every message class a module exports."""
    found: dict[str, str] = {}
    for name in getattr(module, "__all__", []):
        cls = getattr(module, name)
        if inspect.isclass(cls) and issubclass(cls, Message) and cls is not Message:
            wire = cls.model_fields["type"].default
            found[str(wire)] = name
    return found


class TestEveryRpcMethodHasAPlace:
    @pytest.mark.parametrize("method", list(RpcMethod), ids=lambda m: m.value)
    def test_mapped_to_a_member_or_not_reachable(self, method: RpcMethod) -> None:
        where = coverage.rpc_destination(method)
        assert where is not None, (
            f"RpcMethod.{method.name} ({method.value}) is in neither the host model nor "
            "its Not-reachable list. Add the member it is (ember_code.extensions.host."
            "definition) and map it in coverage.RPC — or say in coverage.RPC_UNREACHABLE "
            "why an extension may not do it."
        )
        kind, target = where
        if kind == "member":
            assert member_exists(target), f"{method.value} maps to {target}, which is not a member"
        else:
            assert target in coverage.rows(), f"{method.value}: no Not-reachable row {target!r}"

    def test_nothing_is_both(self) -> None:
        assert not set(coverage.RPC) & set(coverage.RPC_UNREACHABLE)


class TestEveryUiActionHasAPlace:
    def test_every_action_the_ui_sends(self) -> None:
        actions = wire_types(fe_actions)
        missing = sorted(
            wire
            for wire in actions
            if wire not in coverage.FE_ACTIONS and wire not in coverage.FE_ACTIONS_UNREACHABLE
        )
        assert not missing, f"UI actions with no member and no reason: {missing}"
        for wire, target in coverage.FE_ACTIONS.items():
            assert wire in actions, f"coverage names {wire!r}, which the UI does not send"
            assert member_exists(target), f"{wire} maps to {target}, not a member"
        for row in coverage.FE_ACTIONS_UNREACHABLE.values():
            assert row in coverage.rows()


class TestEveryBroadcastHasAPlace:
    def test_every_event_and_mirroring_message(self) -> None:
        broadcasts = {**wire_types(be_events), **wire_types(mirroring)}
        missing = sorted(wire for wire in broadcasts if wire not in coverage.BROADCASTS)
        assert not missing, f"broadcasts with no member: {missing}"
        for wire, target in coverage.BROADCASTS.items():
            assert wire in broadcasts, f"coverage names {wire!r}, which igni does not broadcast"
            assert member_exists(target), f"{wire} maps to {target}, not a member"

    def test_the_value_objects_inside_broadcasts(self) -> None:
        parts = [
            name
            for module in (be_events, mirroring)
            for name in getattr(module, "__all__", [])
            if inspect.isclass(getattr(module, name))
            and not issubclass(getattr(module, name), Message)
        ]
        assert sorted(parts) == sorted(coverage.BROADCAST_PARTS)
        for target in coverage.BROADCAST_PARTS.values():
            assert member_exists(target)

    @pytest.mark.parametrize(
        "channel", [c for c in PushChannel if c not in coverage.PUSH_NOT_A_CHANNEL], ids=str
    )
    def test_every_push_channel(self, channel: PushChannel) -> None:
        if channel in coverage.PUSH_UNREACHABLE:
            assert coverage.PUSH_UNREACHABLE[channel] in coverage.rows()
            return
        assert channel in coverage.PUSH, f"push channel {channel.value!r} has no member"
        assert member_exists(coverage.PUSH[channel])


class TestTheExistingEventsAreSignals:
    """The four host events extensions had before the model keep their names."""

    @pytest.mark.parametrize(
        ("topic", "signal"),
        [
            (HostTopic.SESSION_SWITCHED, "sessions.current_changed"),
            (HostTopic.WORKSPACE_HEAD_CHANGED, "workspace.head_changed"),
            (HostTopic.WORKSPACE_FILES_CHANGED, "workspace.files_changed"),
            (HostTopic.EXTENSION_STATE_CHANGED, "extensions.state_changed"),
        ],
    )
    def test_topic_is_the_signals_wire_name(self, topic: HostTopic, signal: str) -> None:
        spec, member = HOST_SCHEMA.resolve(signal)
        assert isinstance(member, SignalSpec)
        assert HOST_SCHEMA.wire_name(spec, member) == topic.value

    def test_every_topic_is_a_signal(self) -> None:
        for topic in HostTopic:
            assert HOST_SCHEMA.signal_by_wire(topic.value) is not None, topic


class TestEveryMemberIsAnsweredOrSaysWhy:
    async def test_stage_one_members_are_implemented_and_stage_two_are_not(
        self, tmp_path: Path
    ) -> None:
        rig = await HostRig(tmp_path).start()
        try:
            assert rig.model is not None
            built = rig.model.implemented()
        finally:
            await rig.stop()
        missing: list[str] = []
        premature: list[str] = []
        for spec in HOST_SCHEMA.objects:
            prefix = f"{spec.path}." if spec.path else ""
            keys: list[tuple[tuple[str, str], int]] = []
            for prop in spec.properties:
                keys.append((("get", f"{prefix}{prop.name}"), prop.stage))
                if prop.writable:
                    keys.append((("set", f"{prefix}{prop.name}"), prop.stage))
            for method in spec.methods:
                keys.append((("call", f"{prefix}{method.name}"), method.stage))
            for key, stage in keys:
                if stage == 1 and key not in built:
                    missing.append(f"{key[0]} {key[1]}")
                if stage == 2 and key in built:
                    premature.append(f"{key[0]} {key[1]}")
        assert not missing, f"stage-1 members with no implementation: {missing}"
        assert not premature, f"stage-2 members implemented without being marked built: {premature}"

    def test_nothing_is_implemented_that_the_schema_does_not_define(self) -> None:
        for members_class in ALL_MEMBERS:
            for name in dir(members_class):
                key = getattr(getattr(members_class, name, None), "_host_member", None)
                if key is None:
                    continue
                op, path = key
                spec, member = HOST_SCHEMA.resolve(path)
                if op == "call":
                    assert isinstance(member, MethodSpec), key
                else:
                    assert isinstance(member, PropertySpec), key
                    if op == "set":
                        assert member.writable, key

    def test_every_object_permission_is_declared(self) -> None:
        names = HOST_SCHEMA.permission_names()
        for spec in HOST_SCHEMA.objects:
            assert spec.permission is None or spec.permission in names


class TestTheSpecSaysWhatTheCodeDoes:
    """The *Not reachable* table in the spec is :data:`coverage.NOT_REACHABLE`."""

    def test_every_row_is_in_the_spec(self) -> None:
        section = DOC.read_text().split("## Not reachable", 1)[1].split("\n## ", 1)[0]
        rows = [
            line for line in section.splitlines() if line.startswith("| ") and "---" not in line
        ]
        documented = {re.sub(r"\s+", " ", row) for row in rows[1:]}
        for row in coverage.NOT_REACHABLE:
            line = f"| {row.what} | {row.why} |"
            assert re.sub(r"\s+", " ", line) in documented, f"not in the spec: {line}"
        assert len(documented) == len(coverage.NOT_REACHABLE)

    def test_the_stage_two_members_are_listed_in_the_spec(self) -> None:
        text = DOC.read_text()
        for member in HOST_SCHEMA.stage_two():
            assert f"`{member}`" in text, f"stage-2 member {member} is not named in the spec"
