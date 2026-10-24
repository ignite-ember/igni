"""Who hears which signal, and with what.

The rule from :mod:`ember_code.extensions.events` holds here too: the
grant is checked where a signal is dispatched, never where it is
received. A subscriber names the signals it wants; the bus delivers one
only when the extension's manifest grants the object it belongs to, and
only the part of it the extension may see — a changed file outside its
``workspacePaths`` is not in the list it is handed.

``host.changed`` is produced here, from the others: whenever a signal
fires, every property it reports a change of is announced as
``Change{path, value}`` to the subscribers of ``changed`` that may see
that property.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from ember_code.extensions.host.grants import HostGrants
from ember_code.extensions.host.spec import HostSchema, ObjectSpec, SignalSpec
from ember_code.extensions.host.wire import SignalMessage

logger = logging.getLogger(__name__)

#: ``changed`` — the root signal every property change is also sent as.
CHANGED = "changed"

#: What a producer may hand instead of one value for everyone: a
#: function of the listener's grants, returning ``None`` for "nothing
#: this one may see".
Content = Callable[[HostGrants], Any]


class Sink(Protocol):
    """Where one subscriber's signals go: a socket, or a page."""

    def deliver(self, message: SignalMessage) -> None: ...


class Subscriber:
    """One connection of one extension, and what it asked to hear."""

    def __init__(self, extension_id: str, sink: Sink) -> None:
        self.extension_id = extension_id
        self.sink = sink
        self.wires: set[str] = set()


class SubscribeOutcome(dict[str, list[str]]):
    """``{subscribed, not_granted, unknown}`` — told to the SDK so it can
    warn its author, never an error: subscribing to something ungranted
    is allowed, and nothing arrives."""


class SignalBus:
    """Subscriptions, and delivery that checks each one."""

    def __init__(
        self,
        schema: HostSchema,
        grants_for: Callable[[str], HostGrants | None],
    ) -> None:
        self._schema = schema
        self._grants_for = grants_for
        self._subscribers: list[Subscriber] = []
        # signal wire → (object, signal)
        self._signals: dict[str, tuple[ObjectSpec, SignalSpec]] = {}
        # signal wire → [(object, property name)] whose change it reports
        self._reports: dict[str, list[tuple[ObjectSpec, str]]] = {}
        for spec in schema.objects:
            for sig in spec.signals:
                self._signals[schema.wire_name(spec, sig)] = (spec, sig)
        for spec in schema.objects:
            for prop in spec.properties:
                for ref in prop.changed_by:
                    if "." in ref:
                        target_path, _, name = ref.rpartition(".")
                    else:
                        target_path, name = spec.path, ref
                    target = schema.object(target_path)
                    if target is None:
                        continue
                    found = next((s for s in target.signals if s.name == name), None)
                    if found is None:
                        continue
                    wire = schema.wire_name(target, found)
                    self._reports.setdefault(wire, []).append((spec, prop.name))

    # ── Subscribers ───────────────────────────────────────────────

    def attach(self, extension_id: str, sink: Sink) -> Subscriber:
        subscriber = Subscriber(extension_id, sink)
        self._subscribers.append(subscriber)
        return subscriber

    def detach(self, subscriber: Subscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    def subscribe(self, subscriber: Subscriber, wires: list[str]) -> SubscribeOutcome:
        grants = self._grants_for(subscriber.extension_id)
        outcome = SubscribeOutcome(subscribed=[], not_granted=[], unknown=[])
        for wire in wires:
            found = self._signals.get(wire)
            if found is None:
                outcome["unknown"].append(wire)
                continue
            spec, sig = found
            subscriber.wires.add(wire)
            if grants is None or not grants.allows(spec.permission_of(sig)):
                outcome["not_granted"].append(wire)
            else:
                outcome["subscribed"].append(wire)
        return outcome

    def unsubscribe(self, subscriber: Subscriber, wires: list[str]) -> None:
        subscriber.wires.difference_update(wires)

    def wanted(self, wire: str) -> bool:
        """Is anybody entitled listening — so a producer can skip work."""
        found = self._signals.get(wire)
        if found is None:
            return False
        spec, sig = found
        permission = spec.permission_of(sig)
        for subscriber in self._subscribers:
            if wire not in subscriber.wires and CHANGED not in subscriber.wires:
                continue
            grants = self._grants_for(subscriber.extension_id)
            if grants is not None and grants.allows(permission):
                return True
        return False

    # ── Delivery ──────────────────────────────────────────────────

    def emit(
        self,
        wire: str,
        value: Any = None,
        *,
        content: Content | None = None,
        only: str | None = None,
    ) -> list[str]:
        """Fire one signal. Returns the extensions told.

        ``content`` replaces ``value`` with what each listener may see;
        ``only`` addresses one extension — for a signal about that
        extension alone, which telling the others would leak.
        """
        found = self._signals.get(wire)
        if found is None:
            raise KeyError(f"{wire!r} is not a signal of the host model")
        spec, sig = found
        if sig.stage != 1:
            return []
        permission = spec.permission_of(sig)
        told: list[str] = []
        for subscriber in list(self._subscribers):
            if only is not None and subscriber.extension_id != only:
                continue
            grants = self._grants_for(subscriber.extension_id)
            if grants is None:
                continue
            seen = self._seen(grants, value, content)
            if seen is _NOTHING:
                continue
            if wire in subscriber.wires and grants.allows(permission):
                self._deliver(subscriber, SignalMessage(signal=wire, value=seen))
                told.append(subscriber.extension_id)
            if CHANGED in subscriber.wires:
                for owner, prop_name in self._reports.get(wire, []):
                    prop = owner.member(prop_name)
                    if prop is None or not grants.allows(owner.permission_of(prop)):
                        continue
                    path = f"{owner.path}.{prop_name}" if owner.path else prop_name
                    window = seen.get("window") if isinstance(seen, dict) else None
                    change = {"path": path, "value": seen, "window": window}
                    self._deliver(subscriber, SignalMessage(signal=CHANGED, value=change))
        return told

    @staticmethod
    def _seen(grants: HostGrants, value: Any, content: Content | None) -> Any:
        if content is None:
            return value
        try:
            seen = content(grants)
        except Exception:
            logger.exception("host model: content for a signal failed")
            return _NOTHING
        return _NOTHING if seen is None else seen

    @staticmethod
    def _deliver(subscriber: Subscriber, message: SignalMessage) -> None:
        try:
            subscriber.sink.deliver(message)
        except Exception:
            logger.exception(
                "host model: delivering %s to %s failed", message.signal, subscriber.extension_id
            )


class _Nothing:
    """Nothing this listener may see."""


_NOTHING: Any = _Nothing()

__all__ = ["CHANGED", "Content", "SignalBus", "Sink", "SubscribeOutcome", "Subscriber"]
