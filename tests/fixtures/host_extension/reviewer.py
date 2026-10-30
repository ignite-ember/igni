"""An extension process that uses the host object model, for the tests.

Started by ``tests/test_an_extension_process_reaches_the_host.py`` with
the environment igni gives a process (``IGNI_HOST_URL``,
``IGNI_HOST_SECRET``) and the SDK on its path. It prints one JSON line per
thing it saw, which is how the test reads it.
"""

from __future__ import annotations

import asyncio
import json

from igni_extension import HostError, IgniExtension
from igni_extension.host_model import Run, SessionChange

ext = IgniExtension("acme.reviewer")


def say(**fields: object) -> None:
    print(json.dumps(fields), flush=True)


@ext.on(ext.host.sessions.current_changed)
async def follow(change: SessionChange) -> None:
    # A handler reads and writes the model while it handles the signal.
    processing = await ext.host.chat.processing
    view_id = await ext.host.chat.append(
        {"root": "c", "elements": {"c": {"type": "Card", "props": {"title": change.session_id}}}}
    )
    say(signal="current_changed", session_id=change.session_id, processing=processing, view=view_id)


@ext.on(ext.host.chat.run_completed)
async def reviewed(run: Run) -> None:
    say(signal="run_completed", run_id=run.run_id, changed_files=run.changed_files)


async def main() -> None:
    await ext.connect()
    say(ready=True, granted=ext.connection.granted, schema=ext.connection.schema_version)
    try:
        await ext.host.mcp.servers
    except HostError as exc:
        say(refused=exc.code)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
