"""An extension process that acts on its service's events, for the tests.

Started by ``tests/test_a_service_event_opens_a_chat_and_starts_a_run.py``
with the environment igni gives a process and the SDK on its path. Its
handler is the path-2 shape (``docs/CLOUD.md`` §6): igni posts nothing
for an event, so the handler opens a titled chat of its own — without
moving the person's window — and posts there, which starts the run. It
prints one JSON line per thing it did, which is how the test reads it.
"""

from __future__ import annotations

import asyncio
import json

from igni_extension import IgniExtension, ServiceEvent

ext = IgniExtension("acme.indexer")


def say(**fields: object) -> None:
    print(json.dumps(fields), flush=True)


@ext.on_service_event("index.ready")
async def index_ready(event: ServiceEvent) -> None:
    repo = event.payload["repo"]
    created = await ext.host.sessions.create(title=f"Index: {repo}")
    await ext.host.sessions.chat(created.session_id).post(
        f"The code index for {repo} is ready; review the open changes."
    )
    say(created=created.session_id, event=event.event_id)


async def main() -> None:
    await ext.connect()
    say(ready=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
