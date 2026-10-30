"""Notifications are the floor's: one list, one count per extension.

igni server's ``docs/service-identity.md`` §10. What igni server
delivered to whoever is signed in and what an extension on this machine
told the host are listed together, counted together per extension —
that sum is the badge on the extension — and marked read where each
lives: igni server for its own, this machine for the local ones.

Driven against a fake igni server (``tests/notification_helpers.py``)
and through the RPC handler the client calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.backend.rpc_handlers.base import RpcHandlerContext
from ember_code.backend.rpc_handlers.notifications import NotificationsRpcHandler
from ember_code.extensions.service_token import IgniSession
from ember_code.notifications.center import NotificationCenter
from ember_code.notifications.local_store import LocalNotificationStore
from ember_code.notifications.schemas import (
    ExtensionNotice,
    MarkReadRequest,
    NotificationCounts,
    NotificationOrigin,
    NotificationTarget,
)
from ember_code.notifications.server_client import (
    IgniNotificationsClient,
    NotificationsUnavailable,
    SignedInServer,
)
from ember_code.protocol.rpc import RpcMethod
from ember_code.protocol.schemas.enums import PushChannel
from tests.notification_helpers import (
    API_URL,
    SESSION,
    FakeNotificationServer,
    server_row,
    signed_in,
)


def local_notice(event_key: str = "export:e1", title: str = "Runbook.pdf") -> ExtensionNotice:
    return ExtensionNotice(
        kind="export_ready",
        title=title,
        body="Your export is ready",
        target=NotificationTarget(page="acme.scan", params={"export": "e1"}),
        event_key=event_key,
    )


class Floor:
    def __init__(self, tmp_path, rows: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        self.server = FakeNotificationServer(rows)
        self.store = LocalNotificationStore(tmp_path / "notifications.db")
        self.announced: list[NotificationCounts] = []
        self.center = NotificationCenter(
            server=signed_in(**kwargs),
            local=self.store,
            announce=self.announced.append,
            transport=self.server.transport,
        )
        self.handler = NotificationsRpcHandler(
            RpcHandlerContext(
                backend=None,
                transport=None,
                login=MagicMock(),
                push=MagicMock(),
                notifications=self.center,
            )
        )


@pytest.fixture
def rows() -> list[dict[str, Any]]:
    return [
        server_row("s-new", source="igni.artifacts", minutes_ago=1),
        server_row("s-old", source="igni.artifacts", minutes_ago=30),
        server_row("s-read", source="acme.scanner", minutes_ago=5, read=True),
    ]


# ── The calls to igni server ────────────────────────────────────────


class TestTheCallsToIgniServer:
    async def test_list_counts_and_read_use_the_persons_session(self, rows):
        server = FakeNotificationServer(rows)
        client = IgniNotificationsClient(signed_in(), transport=server.transport)

        listed = await client.listing()
        counts = await client.counts()
        await client.mark_read(MarkReadRequest(ids=["s-new"]))

        assert [n.id for n in listed] == ["s-new", "s-old", "s-read"]
        assert listed[0].target is not None
        assert listed[0].target.params == {"slug": "runbook", "comment": "c1"}
        assert listed[2].read_at is not None
        assert counts == NotificationCounts(unread=2, by_source={"igni.artifacts": 2})
        assert server.read_bodies == [{"ids": ["s-new"]}]
        assert set(server.authorizations) == {f"Bearer {SESSION}"}

    async def test_the_three_ways_to_mark_read(self, rows):
        server = FakeNotificationServer(rows)
        client = IgniNotificationsClient(signed_in(), transport=server.transport)

        await client.mark_read(MarkReadRequest(ids=["a"]))
        await client.mark_read(MarkReadRequest(source="igni.artifacts"))
        await client.mark_read(MarkReadRequest())

        assert server.read_bodies == [{"ids": ["a"]}, {"source": "igni.artifacts"}, {}]

    async def test_a_refusal_is_an_error_not_an_empty_list(self, rows):
        server = FakeNotificationServer(rows)
        server.status = 500
        client = IgniNotificationsClient(signed_in(), transport=server.transport)

        with pytest.raises(NotificationsUnavailable):
            await client.listing()

    async def test_params_that_are_not_strings_arrive_as_text(self):
        server = FakeNotificationServer(
            [server_row("s1", target={"page": "p", "params": {"n": 3, "on": True}})]
        )
        client = IgniNotificationsClient(signed_in(), transport=server.transport)

        [row] = await client.listing()

        assert row.target is not None
        assert row.target.params == {"n": "3", "on": "true"}


# ── One list ───────────────────────────────────────────────────────


async def test_both_paths_are_listed_newest_first(tmp_path, rows):
    floor = Floor(tmp_path, rows)
    floor.center.keep_local("acme.scanner", local_notice())

    result = await floor.handler.notifications_list({})

    assert [n.id for n in result.notifications][1:] == ["s-new", "s-read", "s-old"]
    newest = result.notifications[0]
    assert newest.origin is NotificationOrigin.LOCAL
    assert newest.source == "acme.scanner"
    server_new = result.notifications[1]
    assert server_new.origin is NotificationOrigin.SERVER
    assert server_new.actor == "ada@acme.com"
    assert server_new.target is not None and server_new.target.page == "igni.artifacts"
    assert [n.read for n in result.notifications] == [False, False, True, False]
    assert result.server_error == ""


async def test_an_unreachable_server_keeps_what_was_last_read(tmp_path, rows):
    floor = Floor(tmp_path, rows)
    await floor.center.refresh()

    floor.server.status = 503
    result = await floor.handler.notifications_list({})

    assert "503" in result.server_error
    assert {n.id for n in result.notifications} == {"s-new", "s-old", "s-read"}
    assert result.counts.unread == 2


async def test_signed_out_lists_only_this_machines_own(tmp_path, rows):
    floor = Floor(tmp_path, rows, session=None)
    floor.center.keep_local("acme.scanner", local_notice())

    result = await floor.handler.notifications_list({})

    assert [n.origin for n in result.notifications] == [NotificationOrigin.LOCAL]
    assert floor.server.requests == []
    assert result.server_error == ""


async def test_a_row_names_its_organisation_when_the_person_is_in_several(tmp_path):
    floor = Floor(
        tmp_path,
        [
            server_row("s-acme", org_id="org-acme", minutes_ago=1),
            server_row("s-globex", org_id="org-globex", minutes_ago=2),
        ],
    )
    floor.server.organizations = [
        {"id": "org-acme", "display_name": "Acme", "slug": "acme", "role": "member"},
        {"id": "org-globex", "display_name": "Globex", "slug": "globex", "role": "admin"},
    ]

    result = await floor.handler.notifications_list({})

    assert [(n.org_id, n.org_name) for n in result.notifications] == [
        ("org-acme", "Acme"),
        ("org-globex", "Globex"),
    ]
    # Read once per sign-in, not on every listing.
    await floor.handler.notifications_list({})
    assert floor.server.calls("/v1/portal/me") == 1


async def test_with_one_organisation_no_row_names_it(tmp_path):
    floor = Floor(tmp_path, [server_row("s-acme", org_id="org-acme")])

    result = await floor.handler.notifications_list({})

    assert [(n.org_id, n.org_name) for n in result.notifications] == [("org-acme", "")]


async def test_not_knowing_the_organisations_costs_the_name_not_the_list(tmp_path):
    floor = Floor(tmp_path, [server_row("s-acme", org_id="org-acme")])
    floor.server.me_status = 503

    result = await floor.handler.notifications_list({})

    assert [n.id for n in result.notifications] == ["s-acme"]
    assert result.notifications[0].org_name == ""
    assert result.server_error == ""


# ── One count per extension ─────────────────────────────────────────


async def test_the_counts_add_both_paths_by_source(tmp_path, rows):
    floor = Floor(tmp_path, rows)
    await floor.center.refresh()
    floor.center.keep_local("acme.scanner", local_notice())
    floor.center.keep_local("igni.artifacts", local_notice("export:e2"))

    counts = floor.handler.notifications_counts({})

    assert counts == NotificationCounts(
        unread=4, by_source={"igni.artifacts": 3, "acme.scanner": 1}
    )
    # And that is what the client was last told.
    assert floor.announced[-1] == counts


async def test_counting_asks_igni_server_nothing(tmp_path, rows):
    floor = Floor(tmp_path, rows)

    floor.handler.notifications_counts({})

    assert floor.server.requests == []


def test_counts_add_source_by_source():
    a = NotificationCounts(unread=3, by_source={"x": 2, "y": 1})
    b = NotificationCounts(unread=2, by_source={"y": 1, "z": 1})

    assert a.plus(b) == NotificationCounts(unread=5, by_source={"x": 2, "y": 2, "z": 1})


# ── Marking read, where each lives ──────────────────────────────────


async def test_marking_ids_splits_them_between_the_two(tmp_path, rows):
    floor = Floor(tmp_path, rows)
    kept = floor.center.keep_local("acme.scanner", local_notice())
    assert kept is not None

    result = await floor.handler.notifications_mark_read({"ids": [kept.id, "s-new"]})

    assert floor.server.read_bodies == [{"ids": ["s-new"]}]
    assert floor.store.counts().unread == 0
    assert result.counts == NotificationCounts(unread=1, by_source={"igni.artifacts": 1})
    assert floor.announced[-1] == result.counts


async def test_marking_a_source_marks_it_on_both(tmp_path, rows):
    floor = Floor(tmp_path, rows)
    floor.center.keep_local("igni.artifacts", local_notice())

    result = await floor.handler.notifications_mark_read({"source": "igni.artifacts"})

    assert floor.server.read_bodies == [{"source": "igni.artifacts"}]
    assert result.counts.unread == 0


async def test_marking_everything(tmp_path, rows):
    floor = Floor(tmp_path, rows)
    floor.center.keep_local("acme.scanner", local_notice())

    result = await floor.handler.notifications_mark_read({})

    assert floor.server.read_bodies == [{}]
    assert result.counts.unread == 0


async def test_a_server_that_cannot_mark_says_so(tmp_path, rows):
    floor = Floor(tmp_path, rows)
    await floor.center.refresh()
    floor.server.status = 500

    result = await floor.handler.notifications_mark_read({"ids": ["s-new"]})

    assert "500" in result.server_error
    assert result.counts.unread == 2


# ── The push every badge reads ──────────────────────────────────────


def test_the_counts_reach_the_client_on_their_own_channel():
    sent: list[tuple[str, dict]] = []
    bridge = PushNotificationBridge(transport=MagicMock(), loop=None, queue=[])
    bridge._schedule_push = lambda channel, payload: sent.append((channel, payload))  # type: ignore[method-assign]

    bridge.notifications_changed(NotificationCounts(unread=2, by_source={"igni.artifacts": 2}))

    assert sent == [
        (
            PushChannel.NOTIFICATIONS_CHANGED,
            {"counts": {"unread": 2, "by_source": {"igni.artifacts": 2}}},
        )
    ]


def test_the_three_rpcs_are_the_handlers():
    handler = NotificationsRpcHandler(
        RpcHandlerContext(backend=None, transport=None, login=MagicMock(), push=MagicMock())
    )

    assert set(handler.methods()) == {
        RpcMethod.NOTIFICATIONS_LIST,
        RpcMethod.NOTIFICATIONS_COUNTS,
        RpcMethod.NOTIFICATIONS_MARK_READ,
    }


async def test_a_backend_without_a_center_answers_empty():
    handler = NotificationsRpcHandler(
        RpcHandlerContext(backend=None, transport=None, login=MagicMock(), push=MagicMock())
    )

    assert (await handler.notifications_list({})).notifications == []
    assert handler.notifications_counts({}).unread == 0


def test_no_artifact_rpc_answers_for_notifications_any_more():
    names = {m.value for m in RpcMethod}

    assert not {"artifact_notifications", "artifact_mark_seen"} & names


async def test_a_session_that_ended_takes_its_count_with_it(tmp_path, rows):
    """``/logout`` clears the credentials without telling the center; the
    badge must not keep the last person's number."""
    session: list[str | None] = ["s"]
    floor = Floor(tmp_path, rows)
    floor.center._server = SignedInServer(  # type: ignore[attr-defined]
        api_url=lambda: API_URL,
        session=lambda: None if session[0] is None else IgniSession(access_token=session[0]),
    )
    await floor.center.refresh()
    floor.center.keep_local("acme.scanner", local_notice())
    assert floor.center.counts().unread == 3

    session[0] = None

    assert floor.center.counts() == NotificationCounts(unread=1, by_source={"acme.scanner": 1})
