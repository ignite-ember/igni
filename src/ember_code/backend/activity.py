"""Whether a backend is doing anything — what decides if its VM may sleep.

``docs/CLOUD.md``. A producer's VM is suspended when nothing runs in it.
A run that waits only on a person — the main agent paused on an
approval, or a ``spawn_agent`` call whose sub-agent is paused on one —
does nothing until the person decides: it counts as **waiting**, not
running, and the VM may be suspended. The snapshot keeps the waiting run
frozen exactly where it is; the decision, delivered to the woken VM,
simply continues it. (Should the VM be lost instead, the approval and
what hangs from it are in the database: ``ColdSpawnResume``.)

:class:`Activity` is that state for the whole backend, as the host
object model's ``app.activity`` reports it to the node agent.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ActivityState(StrEnum):
    #: Nothing running and nothing waiting.
    IDLE = "idle"
    #: Nothing running; one or more approvals wait on a person.
    WAITING = "waiting"
    #: A run is working: a model call, a tool, a sub-agent that is not paused.
    RUNNING = "running"


class SessionActivity(BaseModel):
    session_id: str
    state: ActivityState
    waiting_approvals: int = 0

    @classmethod
    def of(
        cls,
        session_id: str,
        *,
        run_in_progress: bool,
        waiting_approvals: int,
        blocked_spawns: int,
        active_subagent_runs: int,
    ) -> SessionActivity:
        """The session's state from what its backend can see.

        A run in progress is only waiting when every sub-agent run it
        has going is blocked on a person (and at least one is): the
        main agent is then inside ``spawn_agent`` calls that do nothing
        until a decision.
        """
        if run_in_progress:
            only_waiting = blocked_spawns > 0 and blocked_spawns >= active_subagent_runs
            state = ActivityState.WAITING if only_waiting else ActivityState.RUNNING
        elif waiting_approvals:
            state = ActivityState.WAITING
        else:
            state = ActivityState.IDLE
        return cls(session_id=session_id, state=state, waiting_approvals=waiting_approvals)


class Activity(BaseModel):
    """The backend as a whole: running if any session runs."""

    state: ActivityState
    #: Whether the VM may be suspended now: nothing is running.
    suspendable: bool
    waiting_approvals: int = 0
    sessions: list[SessionActivity] = Field(default_factory=list)

    @classmethod
    def of(cls, sessions: list[SessionActivity]) -> Activity:
        states = {s.state for s in sessions}
        if ActivityState.RUNNING in states:
            state = ActivityState.RUNNING
        elif ActivityState.WAITING in states:
            state = ActivityState.WAITING
        else:
            state = ActivityState.IDLE
        return cls(
            state=state,
            suspendable=state is not ActivityState.RUNNING,
            waiting_approvals=sum(s.waiting_approvals for s in sessions),
            sessions=sessions,
        )


__all__ = ["Activity", "ActivityState", "SessionActivity"]
