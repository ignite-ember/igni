"""Agent activity widget — compact live display of orchestrator + child agents."""

import time
from collections import OrderedDict

from pydantic import BaseModel, Field
from textual.timer import Timer
from textual.widgets import Static

from ember_code.frontend.tui.widgets._constants import SPINNER_FRAMES
from ember_code.frontend.tui.widgets._formatting import format_elapsed_time, format_token_count

_MAX_DISPLAY_LINES = 4


class AgentState(BaseModel):
    """Tracks the live state of a single agent in a run."""

    name: str
    run_id: str
    parent_run_id: str | None = None
    model: str = ""
    start_time: float = Field(default_factory=time.monotonic)
    status: str = "running"  # "running" | "done" | tool name like "Shell"
    input_tokens: int = 0
    output_tokens: int = 0
    end_time: float | None = None

    @property
    def elapsed(self) -> float:
        end = self.end_time or time.monotonic()
        return end - self.start_time

    @property
    def is_running(self) -> bool:
        return self.status != "done"


class AgentActivityWidget(Static):
    """Compact live display of orchestrator + child agent activity.

    Shows the orchestrator on line 1 with elapsed time, and up to 3
    child agent lines showing name, status, time, and tokens.

    Implements the same public API as SpinnerWidget (set_label, set_tokens,
    stop) so it can be used as a drop-in replacement.
    """

    DEFAULT_CSS = """
    AgentActivityWidget {
        height: auto;
        margin: 0 0 0 2;
    }
    """

    def __init__(self, label: str = "Thinking"):
        self._label = label
        self._frame = 0
        self._agents: OrderedDict[str, AgentState] = OrderedDict()
        self._orchestrator_id: str | None = None
        self._timer: Timer | None = None
        self._stopped = False
        # When an event last bumped the label — used to show "(idle Ns)" when
        # the agent is silent for a while so the user can tell it's still
        # waiting on the model rather than stuck.
        self._label_set_at: float = time.monotonic()
        super().__init__(self._format())

    def on_mount(self) -> None:
        self._timer = self.set_interval(1 / 12, self._tick)

    def _tick(self) -> None:
        if self._stopped:
            return
        self._frame = (self._frame + 1) % len(SPINNER_FRAMES)
        self.update(self._format())

    # ── SpinnerWidget compatibility ────────────────────────────────

    def set_label(self, label: str) -> None:
        self._label = label
        self._label_set_at = time.monotonic()
        if not self._stopped:
            self.update(self._format())

    def set_tokens(self, tokens: int) -> None:
        pass  # tokens tracked per-agent, not globally

    def is_running(self) -> bool:
        return not self._stopped

    def stop(self) -> None:
        self._stopped = True
        if self._timer:
            self._timer.stop()
            self._timer = None

    # ── Agent lifecycle events ─────────────────────────────────────

    def on_agent_started(
        self,
        name: str,
        run_id: str,
        parent_run_id: str | None = None,
        model: str = "",
    ) -> None:
        if run_id in self._agents:
            return
        state = AgentState(
            name=name,
            run_id=run_id,
            parent_run_id=parent_run_id,
            model=model,
        )
        self._agents[run_id] = state
        # First agent with no parent is the orchestrator
        if self._orchestrator_id is None and not parent_run_id:
            self._orchestrator_id = run_id

    def on_agent_tool_started(self, run_id: str, tool_name: str) -> None:
        agent = self._agents.get(run_id)
        if agent:
            agent.status = tool_name

    def on_agent_tool_completed(self, run_id: str) -> None:
        agent = self._agents.get(run_id)
        if agent and agent.status != "done":
            agent.status = "running"

    def on_agent_tokens(self, run_id: str, input_tokens: int, output_tokens: int) -> None:
        agent = self._agents.get(run_id)
        if agent:
            agent.input_tokens += input_tokens or 0
            agent.output_tokens += output_tokens or 0

    def on_agent_completed(self, run_id: str) -> None:
        agent = self._agents.get(run_id)
        if agent:
            agent.status = "done"
            agent.end_time = time.monotonic()

    # ── Rendering ──────────────────────────────────────────────────

    def _format(self) -> str:
        frame = SPINNER_FRAMES[self._frame]

        # No agents registered yet — show spinner-style label
        if not self._agents:
            return f"[dim]{frame} {self._label}...[/dim]"

        lines: list[str] = []

        # Line 1: orchestrator with total tokens
        orch = self._agents.get(self._orchestrator_id or "") if self._orchestrator_id else None
        agent = orch or next(iter(self._agents.values()))
        elapsed = format_elapsed_time(agent.elapsed)
        total_in = sum(a.input_tokens for a in self._agents.values())
        total_out = sum(a.output_tokens for a in self._agents.values())
        tokens = ""
        if total_in or total_out:
            tokens = (
                f"  [dim]{format_token_count(total_in)}\u2191 "
                f"{format_token_count(total_out)}\u2193[/dim]"
            )
        if agent.is_running:
            # Surface the current activity label (Thinking / Streaming /
            # Running <tool> / Reasoning / etc.) on the orchestrator line so
            # the user can see what the agent is actually waiting on. After
            # 5s without a label change we mark it "(idle Ns)" — that's the
            # signal that the model is mid-decision with no streaming yet,
            # the moment that previously looked indistinguishable from stuck.
            silent_for = time.monotonic() - self._label_set_at
            if silent_for >= 5.0:
                label_suffix = f" · {self._label} ([dim]idle {int(silent_for)}s[/dim])"
            else:
                label_suffix = f" · [dim]{self._label}[/dim]"
            lines.append(
                f"[bold $accent]{frame}[/bold $accent] [dim]{elapsed}[/dim]  {agent.name}{tokens}{label_suffix}"
            )
        else:
            lines.append(f"[dim]\u2713 {elapsed}  {agent.name}{tokens}[/dim]")

        # Child agents
        children = [a for rid, a in self._agents.items() if rid != self._orchestrator_id]

        if not children:
            return lines[0]

        # Separate running and done
        running = [a for a in children if a.is_running]
        done = [a for a in children if not a.is_running]

        # Budget: 3 lines for children (total 4 with orchestrator)
        budget = _MAX_DISPLAY_LINES - 1

        if len(running) + len(done) <= budget:
            # All fit — show running first, then done
            for a in running:
                lines.append(self._format_child(a))
            for a in done:
                lines.append(self._format_child(a))
        else:
            # Overflow — prioritize running agents
            if len(done) > 0 and len(running) < budget:
                # Show running + summary of done
                for a in running:
                    lines.append(self._format_child(a))
                remaining_budget = budget - len(running)
                if remaining_budget > 1 or len(done) == 1:
                    # Show some done agents
                    for a in done[-remaining_budget:]:
                        lines.append(self._format_child(a))
                else:
                    lines.append(f"  [dim]+ {len(done)} agents done[/dim]")
            else:
                # More running than budget — show most recent running
                for a in running[-(budget - 1) :]:
                    lines.append(self._format_child(a))
                hidden = len(children) - (budget - 1)
                if hidden > 0:
                    lines.append(f"  [dim]+ {hidden} more agents[/dim]")

        return "\n".join(lines)

    def _format_child(self, agent: AgentState) -> str:
        """Format a single child agent line."""
        icon = "\u25b8" if agent.is_running else "\u2713"
        color = "" if agent.is_running else "[dim]"
        end_color = "[/dim]" if not agent.is_running else ""

        name = agent.name[:16]
        status = agent.status[:12]
        elapsed = format_elapsed_time(agent.elapsed)

        tokens = ""
        if agent.input_tokens:
            tokens = f"{format_token_count(agent.input_tokens)}\u2191"

        parts = f"  {color}{icon} {name:<16}  {status:<12}  {elapsed}"
        if tokens:
            parts += f"  {tokens}"
        parts += end_color

        return parts

    def render_text(self) -> str:
        """Plain-text render for tests."""
        if not self._agents:
            return f"{SPINNER_FRAMES[self._frame]} {self._label}..."

        parts = []
        for agent in self._agents.values():
            status = "done" if not agent.is_running else agent.status
            parts.append(f"{agent.name}({status}, {format_elapsed_time(agent.elapsed)})")
        return " | ".join(parts)
