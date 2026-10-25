"""`ext.prompt`: saying which of the extension's conditions hold.

A manifest's prompt contribution can hold only under a condition —

    "prompt": {"replace": [{"target": "main", "source": "file",
                            "from": "plugin/prompts/main.md",
                            "when": "index-covers-head"}]}

— and only the extension can judge it. So the extension says, when it
changes:

    await ext.prompt.hold("index-covers-head", True)

and igni rebuilds the agent with the prompt as it now is. Nothing polls.
The message is `notifications/igni/conditions` on the extension's own MCP
connection, so an extension can only ever speak for its own conditions.
A condition nobody has said holds does not, and none survive a restart:
say them again when the process starts.

Text a contribution takes from a tool (`"source": "tool"`) is fetched
when the extension starts. When what it says changes — a project map
for the commit HEAD moved to — `await ext.prompt.refresh()` has igni
fetch it again and rebuild the agent.
"""

from __future__ import annotations

from igni_extension.notifications import ConditionsNotification, ConditionsParams
from igni_extension.session import HostSession


class PromptConditions:
    """The conditions this extension's prompt contributions wait on."""

    def __init__(self, session: HostSession) -> None:
        self._session = session
        self._holding: dict[str, bool] = {}

    @property
    def holding(self) -> dict[str, bool]:
        """What was last said, by condition."""
        return dict(self._holding)

    async def hold(self, condition: str, holds: bool = True) -> None:
        """Say whether one condition holds. Saying the same again sends nothing."""
        await self.set({condition: holds})

    async def set(self, conditions: dict[str, bool]) -> None:
        """Say several at once; only the ones that changed are sent."""
        changed = {
            name: holds for name, holds in conditions.items() if self._holding.get(name) != holds
        }
        if not changed:
            return
        notification = ConditionsNotification(params=ConditionsParams(conditions=changed))
        self._holding.update(changed)
        await self._session.send(notification)

    async def refresh(self) -> None:
        """Have igni fetch this extension's tool-sourced prompt text again."""
        notification = ConditionsNotification(params=ConditionsParams(conditions={}, refresh=True))
        await self._session.send(notification)
