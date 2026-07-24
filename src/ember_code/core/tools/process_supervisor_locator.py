"""Process-wide :class:`ProcessSupervisor` locator.

Home of :class:`SupervisorRegistry` — an instance-based replacement
for the module-globals ``_default_supervisor`` and
``_default_supervisor_lock`` the audit flagged as classvar-used-as-
singleton (AP1 + Rule 6).

The module exports one named instance (``supervisors``) so the ~6
sync-import call sites that genuinely need a process-wide supervisor
can reach one without threading it through every constructor. The
state itself lives on the :class:`SupervisorRegistry` instance —
constructor, explicit ``set_default`` injection point, and
``reset_for_tests`` lifecycle — so we no longer have a module-level
mutable global; we have an object whose ONE instance happens to be
bound at module scope.

Import-time construction (``supervisors = SupervisorRegistry()``) is
side-effect-free: it just wires the empty slot + a lock. Lazy
supervisor construction happens on first :meth:`default` call, and
:meth:`BackendServer.startup` can call :meth:`set_default` to inject
a pre-configured supervisor so failures surface immediately.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.core.tools.process_supervisor import ProcessSupervisor


class SupervisorRegistry:
    """Instance-based locator for the process-wide
    :class:`ProcessSupervisor`.

    Not a singleton pattern in the metaclass sense — just a small
    class with an instance slot for the supervisor. The module
    binds ONE instance (``supervisors``) so callers have one name
    to import. Tests can either build their own SupervisorRegistry
    (fully isolated) or call :meth:`reset_for_tests` on the module
    instance.
    """

    def __init__(self) -> None:
        self._supervisor: ProcessSupervisor | None = None
        self._lock = threading.Lock()

    def default(self) -> ProcessSupervisor:
        """Return the current supervisor, lazily constructing one
        on first call.

        Prefer :meth:`set_default` from an explicit BE startup step
        so wiring failures surface at boot rather than deferred to
        first use. This lazy path exists so tests / headless
        callers still work without a full startup sequence.
        """
        # Import here to sidestep the import cycle between
        # ``process_supervisor`` (which references us) and this module.
        from ember_code.core.tools.process_supervisor import ProcessSupervisor  # noqa: PLC0415

        with self._lock:
            if self._supervisor is None:
                self._supervisor = ProcessSupervisor()
            return self._supervisor

    def set_default(self, supervisor: ProcessSupervisor) -> None:
        """Explicitly install ``supervisor`` as the process-wide
        instance. Replaces any previously-installed supervisor —
        use case is ``BackendServer.startup`` injecting a pre-
        configured supervisor before any first-use call falls back
        to the lazy path."""
        with self._lock:
            self._supervisor = supervisor

    def reset_for_tests(self) -> None:
        """Drop the current supervisor so the next :meth:`default`
        call builds a fresh one. Test-fixture helper only —
        production BE never calls this."""
        with self._lock:
            self._supervisor = None


# One named instance — the sync-import call sites all reach this
# object rather than a bare function. Not module-level MUTABLE
# state: the mutable state lives inside the object, protected by
# its own lock, and the binding itself is set once at import time.
supervisors = SupervisorRegistry()
