"""``self.busy``: an extension saying it is working inside igni's process.

``docs/CLOUD.md`` §9. An extension's process may do work nobody asked
igni for — indexing a repository, a long sync — and igni's process must
not be suspended under it. The extension takes a busy lease
(``core/activity``) per reason; the lease expires on its own after at
most :attr:`ExtensionBusy.MAX_TTL_SECONDS`, so an extension that hangs
or forgets cannot keep igni awake: it renews by asking again. All of an
extension's leases go when it stops.
"""

from __future__ import annotations

import threading

from ember_code.core.activity import ActivityRegistry, BusyKind, BusyLease, Lease


class ExtensionBusy:
    """Each extension's busy leases, one per reason."""

    #: Default and longest a lease lasts without being renewed. A
    #: deployment's own limit (the VM profile's) is for later.
    MAX_TTL_SECONDS = 600.0

    def __init__(self, registry: ActivityRegistry | None = None) -> None:
        self._registry = registry
        self._leases: dict[tuple[str, str], Lease] = {}
        self._lock = threading.Lock()

    @property
    def registry(self) -> ActivityRegistry:
        return self._registry if self._registry is not None else ActivityRegistry.shared()

    def busy(self, extension_id: str, reason: str, ttl_seconds: float | None = None) -> BusyLease:
        """Take, or renew, ``extension_id``'s lease for ``reason``."""
        ttl = self.MAX_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        ttl = max(1.0, min(float(ttl), self.MAX_TTL_SECONDS))
        key = (extension_id, reason)
        with self._lock:
            held = self._leases.get(key)
        if held is not None:
            renewed = held.renew(ttl)
            if renewed is not None:
                return renewed
        lease = self.registry.hold(BusyKind.EXTENSION, extension_id, reason, ttl=ttl)
        with self._lock:
            self._leases[key] = lease
        found = self.registry.lease(lease.id)
        assert found is not None
        return found

    def done(self, extension_id: str, reason: str) -> None:
        with self._lock:
            lease = self._leases.pop((extension_id, reason), None)
        if lease is not None:
            lease.release()

    def release_all(self, extension_id: str) -> None:
        """The extension stopped: whatever it held goes."""
        with self._lock:
            mine = [key for key in self._leases if key[0] == extension_id]
            leases = [self._leases.pop(key) for key in mine]
        for lease in leases:
            lease.release()


__all__ = ["ExtensionBusy"]
