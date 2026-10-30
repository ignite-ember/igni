"""A card an extension appended comes back from the history as the extension's.

``host.chat.append(view, title)`` records a ``visualization_delta`` whose
payload carries the card's ``title`` and its ``source`` — the extension's
id. A reload rebuilds the card from that record, and it used to rebuild
every card as the visualizer's with no title: the web client then
labelled an extension's card "via visualizer". The turn now carries the
title and the extension's id, which the client turns into its name.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from ember_code.backend.schemas_history import ChatTurn, VisualizationTurn
from ember_code.backend.server_history import ChatHistoryRebuilder
from ember_code.backend.service_item_desk import ServiceView
from ember_code.core.session.event_log_schema import SessionEvent

SPEC = {"root": "t", "elements": {"t": {"type": "Text", "props": {"text": "3 open"}}}}


def _restored(payload: dict[str, object]) -> VisualizationTurn:
    session = SimpleNamespace(
        event_log=[SessionEvent(seq=1, type="visualization_delta", payload=payload)]
    )
    out: list[ChatTurn] = []
    ChatHistoryRebuilder(session)._splice_visualizations(out)  # type: ignore[arg-type]
    [turn] = out
    assert isinstance(turn, VisualizationTurn)
    return turn


def test_an_appended_card_keeps_its_title_and_extension() -> None:
    appended = ServiceView(
        agent_path="ext:acme.claims",
        spec_id="ext-0123456789ab",
        spec_json=json.dumps(SPEC),
        title="Open claims",
        source="acme.claims",
    ).model_dump(by_alias=True)

    turn = _restored(appended)

    assert turn.title == "Open claims"
    assert turn.extension_id == "acme.claims"
    assert turn.source_agent == "acme.claims"
    assert turn.model_dump()["extension_id"] == "acme.claims"


def test_a_visualizer_card_is_still_the_visualizers() -> None:
    drawn = {"agent_path": "main/visualizer", "spec_id": "viz-1", "json": json.dumps(SPEC)}

    turn = _restored(drawn)

    assert turn.source_agent == "visualizer"
    assert turn.extension_id == ""
    assert turn.title == ""
