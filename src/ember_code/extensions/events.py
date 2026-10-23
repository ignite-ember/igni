"""What an extension may be told, and by whom.

An extension could call and could be called back on nothing: the
bridge is ``hello | theme | params | result``, so following anything
meant polling. Artifacts broadcasts ``artifact_waiting`` today and
two host components subscribe; an extension has no such thing.

## Two kinds of event, one envelope

**Host events** are things igni knows — the session changed, HEAD
moved. Their topics come from :data:`VOCABULARY` and an extension
must be granted them.

**An extension's own events** are things its service knows, relayed
to its own frame. Their topics live under ``ext.<id>.`` and need no
grant: an extension is always allowed to talk to itself, and is
never permitted to hear another.

One envelope carries both, because the guest handles them
identically — a topic and a payload — and two mechanisms for one job
is two things to keep in step.

## Subscription is a grant, not a filter

The check happens where an event is dispatched, never where it is
received. A host that broadcast everything and asked each frame to
discard what was not its own would be trusting the frame to discard
it, and the frame is the untrusted party.

## The vocabulary is published, igni's bus is not

"Listen to host events" cannot mean "subscribe to our broadcast
bus". That bus is a refactoring surface; an extension pinned to it
would mean igni could never change it. :data:`VOCABULARY` is the
promise, under EXTENSIBILITY §8, and nothing else crosses.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ember_code.extensions.registry import NAMESPACE, InstalledExtension

logger = logging.getLogger(__name__)

#: A topic: dotted, lowercase, no wildcards. Wildcards belong in a
#: grant, which is read by whoever approves an install; a topic is
#: emitted by code and should name exactly one thing.
TOPIC = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+$")


class HostTopic(StrEnum):
    """Every host event, as a symbol rather than a string.

    A ``StrEnum`` for the reason :class:`~ember_code.protocol.rpc.RpcMethod`
    is one: the value *is* what travels, so nothing changes on the
    wire, and an emitter writing ``HostTopic.WORKSPACE_HEAD_CHANGED``
    fails at import when it is renamed rather than silently
    delivering to nobody. A topic misspelled in a string literal is
    an event an extension waits for forever.

    **Adding a member is a promise under EXTENSIBILITY section 8**,
    with a deprecation window measured in months. The list is short
    on purpose: a missing event costs an extension a poll, and a
    wrong one costs igni a shape it can never change.
    """

    SESSION_SWITCHED = "session.switched"
    WORKSPACE_HEAD_CHANGED = "workspace.head-changed"
    WORKSPACE_FILES_CHANGED = "workspace.files-changed"
    EXTENSION_STATE_CHANGED = "extension.state-changed"


@dataclass(frozen=True, slots=True)
class HostEvent:
    """What one topic means, and what travels with it."""

    topic: HostTopic
    #: When it fires, for the author deciding whether to ask for it.
    description: str
    #: The payload's shape. Named here so it is part of the promise
    #: rather than whatever the emitter happened to have to hand.
    payload: str


#: Everything an extension may subscribe to, described.
#:
#: Every :class:`HostTopic` must appear exactly once —
#: :func:`validate_vocabulary` checks it at import, so a member
#: added without a description fails here rather than reaching an
#: author as a topic nobody can explain.
VOCABULARY: tuple[HostEvent, ...] = (
    HostEvent(
        topic=HostTopic.SESSION_SWITCHED,
        description="The user moved to a different session.",
        payload="{ session_id }",
    ),
    HostEvent(
        topic=HostTopic.WORKSPACE_HEAD_CHANGED,
        description=(
            "The project's git HEAD moved. The event CodeIndex exists to "
            "wait for, so it does not poll `git rev-parse` itself. Requires "
            "`workspace` as well as the topic: a branch name is workspace "
            "information."
        ),
        payload="{ commit_sha, branch }",
    ),
    HostEvent(
        topic=HostTopic.WORKSPACE_FILES_CHANGED,
        description=(
            "Files changed, as git sees them — so never an ignored file, and "
            "never at all outside a repository. Filtered to your "
            "`workspacePaths` and coalesced by the host, so a build does not "
            "become ten thousand events. `truncated` means the list was "
            "capped and you should rescan rather than trust it."
        ),
        payload="{ paths, truncated }",
    ),
    HostEvent(
        topic=HostTopic.EXTENSION_STATE_CHANGED,
        description=(
            "This extension's own health moved — READY, FAILED and why. An "
            "extension is told about itself and never about another."
        ),
        payload="{ state, reason }",
    ),
)

#: By topic string, for the dispatcher and the grant check.
BY_TOPIC: dict[str, HostEvent] = {str(event.topic): event for event in VOCABULARY}


def validate_vocabulary() -> None:
    """Every topic described, exactly once.

    The analogue of :func:`~ember_code.protocol.rpc.validate_rpc_table`,
    and for the same reason: an added-member-but-forgot-to-describe-it
    mistake should surface at import rather than as an author
    reading a list with a hole in it.
    """
    described = [event.topic for event in VOCABULARY]
    missing = [topic for topic in HostTopic if topic not in described]
    if missing:
        raise RuntimeError(
            f"HostTopic members with no VOCABULARY entry: {sorted(str(t) for t in missing)}"
        )
    duplicated = {t for t in described if described.count(t) > 1}
    if duplicated:
        raise RuntimeError(f"described twice: {sorted(str(t) for t in duplicated)}")


validate_vocabulary()


class UnknownTopic(Exception):
    """A topic that is not in the vocabulary and not an extension's own.

    Raised at *emit*, not at subscribe: a typo in a grant is an
    extension asking for something that does not exist, which is
    harmless and reported at install. A typo in an emitted topic is
    igni failing to deliver an event somebody is waiting for.
    """


def own_namespace(extension_id: str) -> str:
    """The prefix an extension's own topics live under."""
    return f"{NAMESPACE}{extension_id}."


def is_own(topic: str, extension_id: str) -> bool:
    """Is this the extension's own event, rather than a host one?"""
    return topic.startswith(own_namespace(extension_id))


def granted(topic: str, *, extension_id: str, grants: list[str]) -> bool:
    """May this extension be told about this topic?

    Its own events need no grant — it is always allowed to talk to
    itself. A host event needs an exact match or a ``*``-suffixed
    prefix, the same grammar ``permissions.rpc`` uses, and for the
    same reason: this list is read by whoever approves the install.

    An extension can never match another's namespace, because a
    grant matching ``ext.`` would have to be written by somebody who
    then has to explain it.
    """
    if is_own(topic, extension_id):
        return True
    if topic.startswith(NAMESPACE):
        # Another extension's. Not grantable at all — not by a
        # wildcard, not by naming it exactly.
        return False
    for grant in grants:
        if grant.endswith("*"):
            if topic.startswith(grant[:-1]):
                return True
        elif topic == grant:
            return True
    return False


def unknown_grants(grants: list[str]) -> list[str]:
    """Grants that match nothing in the vocabulary.

    Reported at install rather than refused: a manifest asking for
    an event igni does not have is usually one written against a
    newer igni, and refusing the install over it would make every
    new event a breaking change for everybody who guessed early.
    """
    stray: list[str] = []
    for grant in grants:
        prefix = grant[:-1] if grant.endswith("*") else None
        if prefix is not None:
            if not any(topic.startswith(prefix) for topic in BY_TOPIC):
                stray.append(grant)
        elif grant not in BY_TOPIC:
            stray.append(grant)
    return stray


class EventRouter:
    """Decides who hears what, and tells only them.

    The whole isolation rule lives here, at dispatch, because this
    is the only place it can live. Anything that posted an event to
    every frame and relied on each to discard what was not its own
    would be relying on the untrusted party to discard it.
    """

    def __init__(self, registry: object) -> None:
        #: The :class:`~ember_code.extensions.registry.ExtensionRegistry`.
        #: Untyped to keep this module importable on its own, which
        #: is what lets the grant rules above be tested without one.
        self._registry = registry
        self._deliver: dict[str, object] = {}

    @staticmethod
    def _check_topic(topic: str) -> None:
        if not TOPIC.match(topic):
            raise UnknownTopic(
                f"{topic!r} is not a topic. Dotted and lowercase, and never a "
                "wildcard — wildcards are grant syntax, and an emitted event "
                "names exactly one thing."
            )
        if not topic.startswith(NAMESPACE) and topic not in BY_TOPIC:
            raise UnknownTopic(
                f"{topic!r} is not in the published vocabulary and is not an "
                "extension's own. Add it to VOCABULARY, which is a promise "
                "under EXTENSIBILITY section 8."
            )

    def connect(self, extension_id: str, deliver: object) -> None:
        """Attach the thing that posts into one extension's frame."""
        self._deliver[extension_id] = deliver

    def disconnect(self, extension_id: str) -> None:
        self._deliver.pop(extension_id, None)

    def audience_entries(self, topic: str) -> list[tuple[str, InstalledExtension]]:
        """The audience with each one's registry entry.

        A producer deciding *what* to send often needs the
        manifest — ``workspace.files-changed`` has to know which
        paths each extension was granted before it can address
        anybody. Reading the registry here rather than handing it
        to every producer keeps one object answering "who is
        installed", which is the object that also checks the grant.
        """
        self._check_topic(topic)
        heard_by: list[tuple[str, InstalledExtension]] = []
        for extension_id in self._registry.ids():  # type: ignore[attr-defined]
            entry = self._registry.get(extension_id)  # type: ignore[attr-defined]
            if entry is None:
                continue
            if granted(
                topic,
                extension_id=extension_id,
                grants=list(entry.manifest.permissions.events),
            ):
                heard_by.append((extension_id, entry))
        return heard_by

    def audience(self, topic: str) -> list[str]:
        """Which installed extensions may be told about ``topic``.

        Separate from :meth:`emit` so that "who would hear this" is
        answerable without sending anything — which is what an
        administrator asking "why did my extension not see that"
        needs, and what the tests assert against.
        """
        return [extension_id for extension_id, _ in self.audience_entries(topic)]

    def emit_to(self, extension_id: str, topic: str, payload: dict[str, object]) -> bool:
        """Tell one extension, if it is entitled. ``True`` if told.

        For an event that is *about* somebody — ``extension.state-changed``
        is one extension's health, and sending it to every extension
        that granted the topic would tell them all about each other.
        That is the isolation rule failing in the one place it is
        easiest to miss: the grant was checked, and the audience was
        still wrong.
        """
        self._check_topic(topic)
        entry = self._registry.get(extension_id)  # type: ignore[attr-defined]
        if entry is None:
            return False
        if not granted(
            topic,
            extension_id=extension_id,
            grants=list(entry.manifest.permissions.events),
        ):
            return False
        deliver = self._deliver.get(extension_id)
        if deliver is None:
            return False
        try:
            deliver(topic, payload)  # type: ignore[operator]
        except Exception:
            logger.exception("delivering %s to %s failed", topic, extension_id)
            return False
        return True

    def emit_each(
        self,
        topic: str,
        payload_for: Callable[[InstalledExtension], dict[str, object] | None],
    ) -> list[str]:
        """Tell everyone entitled, each a payload of its own.

        For a topic whose *content* is not the same for every
        listener. ``workspace.files-changed`` is the case that
        forced it: two extensions can both be granted the topic
        and be entitled to see different parts of the tree, so one
        payload for both hands each of them the other's. The grant
        says whether you may be told; it does not say what.

        ``payload_for`` returns ``None`` for "nothing this one may
        see", which skips rather than sending an empty event — an
        extension granted ``docs/**`` should not be woken every
        time ``src/`` is touched, and an event it has to ignore is
        still an event it had to be given.
        """
        told: list[str] = []
        for extension_id, entry in self.audience_entries(topic):
            deliver = self._deliver.get(extension_id)
            if deliver is None:
                continue
            payload = payload_for(entry)
            if payload is None:
                continue
            try:
                deliver(topic, payload)  # type: ignore[operator]
            except Exception:
                logger.exception("delivering %s to %s failed", topic, extension_id)
                continue
            told.append(extension_id)
        return told

    def emit(self, topic: str, payload: dict[str, object]) -> list[str]:
        """Tell everyone entitled. Returns who was told.

        A delivery that raises does not stop the others: one frame
        being gone is not a reason the rest miss an event.
        """
        told: list[str] = []
        for extension_id in self.audience(topic):
            deliver = self._deliver.get(extension_id)
            if deliver is None:
                continue
            try:
                deliver(topic, payload)  # type: ignore[operator]
            except Exception:
                logger.exception("delivering %s to %s failed", topic, extension_id)
                continue
            told.append(extension_id)
        return told
