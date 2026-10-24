"""Host events, and the isolation rule that defines them.

An extension could call and could be told nothing: the bridge was
`hello | theme | params | result`, so following anything meant
polling. Artifacts broadcast `artifact_waiting` and two host
components subscribed — an extension had no such thing.

The rule that shapes the whole design: **subscription is a grant,
not a filter.** The check happens where an event is dispatched,
never where it is received. Anything that posted to every frame and
asked each to discard what was not its own would be trusting the
frame to discard it, and the frame is the untrusted party.
"""

from __future__ import annotations

import pytest

from ember_code.extensions.events import (
    BY_TOPIC,
    VOCABULARY,
    EventRouter,
    HostTopic,
    UnknownTopic,
    granted,
    unknown_grants,
    validate_vocabulary,
)
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.registry import ExtensionRegistry


def manifest(extension_id: str, events: list[str]) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
            "permissions": {"events": events},
        }
    )


def router(*installed: ExtensionManifest) -> EventRouter:
    registry = ExtensionRegistry()
    for one in installed:
        registry.install(one)
    return EventRouter(registry)


# ── The vocabulary is a list, not a convention ─────────────────────


def test_every_topic_is_described():
    """The analogue of `validate_rpc_table`: a member added without
    a description reaches an author as a topic nobody can explain."""
    validate_vocabulary()
    assert len(VOCABULARY) == len(list(HostTopic))
    assert set(BY_TOPIC) == {str(t) for t in HostTopic}


def test_a_topic_is_a_symbol_not_a_string():
    """`RpcMethod` is a StrEnum because typos surface only at
    runtime. A misspelled topic is an event somebody waits for
    forever."""
    assert HostTopic.WORKSPACE_HEAD_CHANGED == "workspace.head-changed"


# ── Who hears what ─────────────────────────────────────────────────


def test_an_exact_grant_is_heard():
    one = manifest("acme.one", ["workspace.head-changed"])
    assert router(one).audience(HostTopic.WORKSPACE_HEAD_CHANGED) == ["acme.one"]


def test_a_prefix_grant_covers_a_family():
    one = manifest("acme.one", ["workspace.*"])
    heard = router(one)
    assert heard.audience(HostTopic.WORKSPACE_HEAD_CHANGED) == ["acme.one"]
    assert heard.audience(HostTopic.WORKSPACE_FILES_CHANGED) == ["acme.one"]


def test_what_was_not_granted_is_not_heard():
    one = manifest("acme.one", ["workspace.*"])
    assert router(one).audience(HostTopic.SESSION_SWITCHED) == []


def test_granting_nothing_hears_nothing():
    """Deny by default, like every other permission."""
    one = manifest("acme.one", [])
    assert router(one).audience(HostTopic.WORKSPACE_HEAD_CHANGED) == []


# ── The isolation rule ─────────────────────────────────────────────


def test_an_extension_hears_its_own_without_a_grant():
    """It is always allowed to talk to itself."""
    assert granted("ext.acme.one.comment", extension_id="acme.one", grants=[])


def test_an_extension_never_hears_another():
    """Not by a wildcard, and not by naming it exactly. The topic is
    unreachable rather than merely ungranted."""
    for grant in (["ext.*"], ["ext.other.two.*"], ["ext.other.two.comment"]):
        assert not granted("ext.other.two.comment", extension_id="acme.one", grants=grant), grant


def test_two_extensions_do_not_overhear_each_other():
    one = manifest("acme.one", ["workspace.*"])
    two = manifest("other.two", [])
    heard = router(one, two)

    assert heard.audience(HostTopic.WORKSPACE_HEAD_CHANGED) == ["acme.one"]
    assert heard.audience("ext.acme.one.thing") == ["acme.one"]
    assert heard.audience("ext.other.two.thing") == ["other.two"]


# ── Emitting ───────────────────────────────────────────────────────


def test_only_the_audience_is_delivered_to():
    one = manifest("acme.one", ["workspace.*"])
    two = manifest("other.two", [])
    heard = router(one, two)

    got: dict[str, list[str]] = {"acme.one": [], "other.two": []}
    heard.connect("acme.one", lambda topic, _p: got["acme.one"].append(topic))
    heard.connect("other.two", lambda topic, _p: got["other.two"].append(topic))

    heard.emit(HostTopic.WORKSPACE_HEAD_CHANGED, {"commit_sha": "abc"})
    assert got == {"acme.one": ["workspace.head-changed"], "other.two": []}


def test_one_dead_frame_does_not_cost_the_others_an_event():
    one = manifest("acme.one", ["workspace.*"])
    two = manifest("other.two", ["workspace.*"])
    heard = router(one, two)

    def explode(_topic: str, _payload: dict[str, object]) -> None:
        raise RuntimeError("that frame is gone")

    delivered: list[str] = []
    heard.connect("acme.one", explode)
    heard.connect("other.two", lambda t, _p: delivered.append(t))

    told = heard.emit(HostTopic.WORKSPACE_HEAD_CHANGED, {})
    assert told == ["other.two"]
    assert delivered == ["workspace.head-changed"]


def test_an_unconnected_extension_is_in_the_audience_and_not_told():
    """`audience` answers "who may hear this", which is what an
    administrator asking "why did my extension not see that" needs —
    separately from whether a frame happened to be mounted."""
    one = manifest("acme.one", ["workspace.*"])
    heard = router(one)
    assert heard.audience(HostTopic.WORKSPACE_HEAD_CHANGED) == ["acme.one"]
    assert heard.emit(HostTopic.WORKSPACE_HEAD_CHANGED, {}) == []


# ── Emitting something that is not a topic ─────────────────────────


def test_an_unpublished_topic_is_refused_at_emit():
    """At emit, not at subscribe. A typo in a grant is an extension
    asking for something that does not exist, which is harmless. A
    typo in an emitted topic is igni failing to deliver."""
    with pytest.raises(UnknownTopic, match="published vocabulary"):
        router().audience("workspace.nope")


def test_a_wildcard_is_not_a_topic():
    """Wildcards are grant syntax. An emitted event names exactly
    one thing."""
    with pytest.raises(UnknownTopic, match="not a topic"):
        router().audience("workspace.*")


def test_a_grant_matching_nothing_is_reported_not_refused():
    """A manifest asking for an event igni does not have is usually
    one written against a newer igni. Refusing the install over it
    would make every new event a breaking change for whoever guessed
    early."""
    assert unknown_grants(["workspace.*"]) == []
    assert unknown_grants(["nope.thing"]) == ["nope.thing"]


# ── The wire ───────────────────────────────────────────────────────


def test_an_event_is_addressed_to_one_extension_on_the_wire():
    """One channel for all of them, and the payload names the frame.

    Addressed rather than broadcast: who was entitled was decided by
    the router before this, so the client posts it into one frame
    and nothing has to be filtered on the way.
    """
    from ember_code.protocol.schemas.push import push_extension_event

    push = push_extension_event(
        extension_id="acme.one",
        topic=str(HostTopic.WORKSPACE_HEAD_CHANGED),
        payload={"commit_sha": "abc"},
    )
    assert push.channel == "extension_event"
    assert push.payload == {
        "extension_id": "acme.one",
        "topic": "workspace.head-changed",
        "payload": {"commit_sha": "abc"},
    }


def test_an_extensions_own_state_change_reaches_only_itself():
    """The bug this test was written badly enough to find.

    `extension.state-changed` is *one* extension's health. The
    first emitter broadcast it to everyone who had granted the
    topic, which would have told every extension about every other
    one — the isolation rule failing in the place it is easiest to
    miss, because the grant *was* checked and the audience was
    still wrong.

    `emit_to` is the fix: an event that is about somebody goes to
    that somebody.
    """
    one = manifest("acme.one", ["extension.state-changed"])
    two = manifest("other.two", ["extension.state-changed"])
    heard = router(one, two)

    told: list[str] = []
    heard.connect("acme.one", lambda t, _p: told.append(f"acme.one:{t}"))
    heard.connect("other.two", lambda t, _p: told.append(f"other.two:{t}"))

    # acme.one's health changed, so acme.one hears about it.
    assert heard.emit_to("acme.one", HostTopic.EXTENSION_STATE_CHANGED, {"state": "FAILED"})
    assert told == ["acme.one:extension.state-changed"]


def test_a_targeted_event_still_needs_the_grant():
    """Addressing is not authorisation. An extension that did not
    ask to hear about itself is not told."""
    one = manifest("acme.one", [])
    heard = router(one)
    heard.connect("acme.one", lambda _t, _p: None)
    assert not heard.emit_to("acme.one", HostTopic.EXTENSION_STATE_CHANGED, {"state": "FAILED"})


def test_a_targeted_event_to_an_uninstalled_extension_goes_nowhere():
    heard = router()
    assert not heard.emit_to("never.installed", HostTopic.EXTENSION_STATE_CHANGED, {})
