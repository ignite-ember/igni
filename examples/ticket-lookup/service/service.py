"""Ticket lookup's service.

Registers with igni server, ships the extension bundle, answers erasure and
export, and reports what became of the extension's VM profile request.

    uvicorn --factory service:create_app --port 18201
"""

from fastapi import Depends, FastAPI

from igni_service import ExportSection, IgniService, IgniSettings, Person, Subject


def create_app(igni: IgniService | None = None) -> FastAPI:
    """The app; `igni` is a FakeIgni's service in tests, the real one otherwise."""
    igni = igni or IgniService.from_settings(IgniSettings())
    app = FastAPI(lifespan=igni.lifespan)
    igni.install(app)

    # What this service keeps about people: who looked which ticket up.
    lookups: dict[tuple[str, str], list[str]] = {}

    @igni.on_erase
    async def erase(subject: Subject) -> None:
        lookups.pop((subject.org_id, subject.user_id), None)

    @igni.on_export
    async def export(subject: Subject) -> list[ExportSection]:
        rows = [{"ticket": t} for t in lookups.get((subject.org_id, subject.user_id), [])]
        return [ExportSection(name="Ticket lookups", rows=rows)]

    @app.get("/v1/tickets/{ticket_id}")
    async def ticket(ticket_id: str, person: Person = Depends(igni.person)) -> dict:
        lookups.setdefault((person.org_id, person.user_id), []).append(ticket_id)
        return {"id": ticket_id, "looked_up_by": person.email}

    @app.get("/v1/vm-profile")
    async def vm_profile(person: Person = Depends(igni.org_admin)) -> dict:
        """What became of the extension's `vm` request in the caller's organisation."""
        status = await igni.host(person.org_id).vm_profile()
        return status.model_dump(mode="json")

    return app
