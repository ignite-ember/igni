"""The example's tests, written only against what the SDK READMEs document."""

import json
import sys
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "service"))
sys.path.insert(0, str(HERE.parent / "extension" / "src"))

from igni_extension import Caller, CallerKind  # noqa: E402
from igni_extension.testing import ConcurrencyHarness, ConcurrentCall, HostHarness  # noqa: E402
from igni_service.testing import FakeIgni  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


ADA = Caller(kind=CallerKind.PERSON, id="u-ada", org="org-1", group="g-1")
GRACE = Caller(kind=CallerKind.PERSON, id="u-grace", org="org-1", group="g-1")


async def test_the_tool_answers_as_igni_calls_it() -> None:
    from acme_tickets_extension import ext

    async with HostHarness(ext) as host:
        result = await host.call("lookup_ticket", {"ticket_id": "T-1001"}, caller=ADA)
    assert json.loads(result.content[0].text)["title"] == "VPN drops every hour"


async def test_many_callers_at_once() -> None:
    from acme_tickets_extension import ext

    report = await ConcurrencyHarness(ext).run(
        [
            ConcurrentCall(tool="lookup_ticket", arguments={"ticket_id": "T-1001"}, caller=ADA),
            ConcurrentCall(tool="lookup_ticket", arguments={"ticket_id": "T-1002"}, caller=GRACE),
        ]
    )
    assert report.results[0].result != report.results[1].result


async def test_the_service_takes_a_person_and_answers_export_and_erasure() -> None:
    from service import create_app

    fake = FakeIgni(service_url="http://tickets.test")
    fake.add_member("org-1", "u-ada", email="ada@acme.test", name="Ada")
    app = create_app(fake.service())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tickets.test") as http:
        person = {"Authorization": f"Bearer {fake.person_token('u-ada', org_id='org-1')}"}
        r = await http.get("/v1/tickets/T-1001", headers=person)
        assert r.status_code == 200, r.text
        assert r.json()["looked_up_by"] == "ada@acme.test"

        igni = {"Authorization": f"Bearer {fake.igni_token('org-1')}"}
        r = await http.get("/v1/igni/subjects/u-ada/export", headers=igni)
        assert r.status_code == 200, r.text
        assert "T-1001" in r.text
        r = await http.delete("/v1/igni/subjects/u-ada", headers=igni)
        assert r.status_code in (200, 204), r.text
        r = await http.get("/v1/igni/subjects/u-ada/export", headers=igni)
        assert "T-1001" not in r.text
