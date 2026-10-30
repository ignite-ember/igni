"""A file a person uploads, or a service's item brings, reaches a vision model as media.

The composer keeps an upload with ``upload_attachment`` and names it in the
message as ``@<absolute path>``; a service's item is kept the same way by
``PoolChatDesk.keep_file`` and named the same way. ``MediaResolver`` used to
collect only the paths it rewrote — relative, ``~``, a bare filename — so an
absolute path, which there is nothing to rewrite in, was never attached: a
vision model was handed the image's path as a string and nothing else.

An absolute path is attached only from an allowed place: the project's
uploads directory or the workspace. One anywhere else stays text.

Each test runs the turn the way ``RunController`` does: ``PromptBuilder``,
then ``arun`` with the media it built, against an OpenAI-compatible model
whose HTTP transport records the chat-completions request it is sent.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from agno.agent import Agent
from agno.models.openai.like import OpenAILike

from ember_code.backend.prompt_builder import PromptBuilder
from ember_code.backend.schemas_run import PromptBuildResult
from ember_code.backend.server_files import FilesController
from ember_code.backend.service_item_desk import PoolChatDesk
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.utils.media import MediaResolver
from tests.service_item_rig import Desk, Files, Inbox, message

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.7\n%%EOF\n"
SESSION = "s-upload"


class RecordedRequests:
    """An OpenAI-compatible endpoint that keeps every request body it is sent."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "vision-stub",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "seen"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    def model(self) -> OpenAILike:
        return OpenAILike(
            id="vision-stub",
            api_key="unused",
            base_url="http://vision-stub.internal/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )

    def user_parts(self) -> list[Any]:
        """The content of the last user message of the one request sent."""
        assert len(self.bodies) == 1
        user = [m for m in self.bodies[0]["messages"] if m["role"] == "user"][-1]
        content = user["content"]
        return content if isinstance(content, list) else [{"type": "text", "text": content}]

    def parts_of(self, kind: str) -> list[dict[str, Any]]:
        return [p for p in self.user_parts() if p.get("type") == kind]

    def text(self) -> str:
        return "".join(p["text"] for p in self.parts_of("text"))


class Workspace:
    """A project, one chat in it, and the model the chat is set to."""

    def __init__(self, root: Path, *, vision: bool) -> None:
        self.project = root / "project"
        self.project.mkdir()
        self.session = SimpleNamespace(
            project_dir=self.project,
            session_id=SESSION,
            settings=SimpleNamespace(
                storage=SimpleNamespace(database_url=None, data_dir=root),
                models=SimpleNamespace(
                    default="vision-stub", registry={"vision-stub": {"vision": vision}}
                ),
            ),
            inject_learnings=self._nothing_learned,
            _drain_pending_reminders=list,
        )
        self.endpoint = RecordedRequests()

    @staticmethod
    async def _nothing_learned() -> None:
        return None

    def upload(self, filename: str, content: bytes) -> str:
        """What the composer does: ``upload_attachment``, then ``@<path>`` in the text."""
        kept = FilesController(self.session).upload_attachment(  # type: ignore[arg-type]
            filename, base64.b64encode(content).decode()
        )
        assert not kept.error
        return kept.path

    async def turn(self, text: str) -> PromptBuildResult:
        """Build the prompt and run it, as ``RunController`` does."""
        built = await PromptBuilder(self.session).build(  # type: ignore[arg-type]
            text, None, interrupted_summary=None
        )
        media = built.media.to_kwargs() if built.media is not None else {}
        await Agent(model=self.endpoint.model(), telemetry=False).arun(built.message, **media)
        return built


def _png_data_urls(parts: list[dict[str, Any]]) -> list[str]:
    return [p["image_url"]["url"] for p in parts]


# ── A person's upload ─────────────────────────────────────────────


async def test_an_uploaded_image_reaches_the_model_as_an_image(tmp_path: Path):
    space = Workspace(tmp_path, vision=True)
    ref = space.upload("screen shot.png", PNG)

    built = await space.turn(f"What does this show? @{ref} ")

    assert built.media is not None and len(built.media.images) == 1
    images = space.endpoint.parts_of("image_url")
    assert len(images) == 1, "attached once, though the message names it twice"
    assert _png_data_urls(images)[0].endswith(base64.b64encode(PNG).decode())


async def test_an_uploaded_pdf_reaches_the_model_as_a_file(tmp_path: Path):
    space = Workspace(tmp_path, vision=True)
    ref = space.upload("claim.pdf", PDF)

    built = await space.turn(f"Summarise @{ref} ")

    assert built.media is not None
    assert [Path(f.filepath) for f in built.media.files] == [Path(ref)]
    assert len(space.endpoint.parts_of("file")) == 1


async def test_two_uploads_side_by_side_both_reach_the_model(tmp_path: Path):
    space = Workspace(tmp_path, vision=True)
    first = space.upload("before.png", PNG)
    second = space.upload("after.png", PNG)

    await space.turn(f"Compare @{first} @{second} ")

    assert len(space.endpoint.parts_of("image_url")) == 2


async def test_a_model_without_vision_is_given_the_path_and_no_media(tmp_path: Path):
    space = Workspace(tmp_path, vision=False)
    ref = space.upload("screen.png", PNG)

    built = await space.turn(f"What does this show? @{ref} ")

    assert built.media is None
    assert not [i for i in built.info_messages if i.text.startswith(("Attached", "Resolved"))], (
        "nothing was rewritten, nothing attached: the same lines as before"
    )
    assert space.endpoint.parts_of("image_url") == []
    assert ref in space.endpoint.text(), "the path is left for its tools to read"


async def test_an_upload_of_a_kind_with_no_media_form_stays_text(tmp_path: Path):
    space = Workspace(tmp_path, vision=True)
    ref = space.upload("notes.txt", b"plain words")

    built = await space.turn(f"Read @{ref} ")

    assert built.media is None
    assert space.endpoint.parts_of("image_url") == []
    assert ref in space.endpoint.text()


# ── A service's item ──────────────────────────────────────────────


class PoolDesk(Desk):
    """The rig's desk, keeping files with the backend's real ``PoolChatDesk``."""

    def __init__(self, session: SimpleNamespace) -> None:
        super().__init__()

        class Backend:
            def upload_attachment(self, filename: str, content_base64: str) -> Any:
                return FilesController(session).upload_attachment(  # type: ignore[arg-type]
                    filename, content_base64
                )

        class Pool:
            async def get_or_create(self, session_id: str) -> Any:
                return SimpleNamespace(backend=Backend())

        self._pool_desk = PoolChatDesk(SimpleNamespace(pool=Pool()))

    async def keep_file(self, *, session_id: str, filename: str, content: bytes) -> str:
        return await self._pool_desk.keep_file(
            session_id=session_id, filename=filename, content=content
        )


async def test_a_service_attachment_reaches_the_model_as_an_image(
    tmp_path: Path, database_url: DatabaseUrl
):
    space = Workspace(tmp_path, vision=True)
    rig = Inbox(
        database_url,
        desk=PoolDesk(space.session),
        files=Files(**{"a-photo": PNG, "a-form": PDF}),
    )
    await rig.deliver(
        message(
            "m1",
            text="New claim.",
            attachments=[
                {"attachment_id": "a-photo", "filename": "damage.png", "content_type": "image/png"},
                {
                    "attachment_id": "a-form",
                    "filename": "claim.pdf",
                    "content_type": "application/pdf",
                },
            ],
        )
    )
    [posted] = rig.desk.posts

    built = await space.turn(posted.text)

    assert built.media is not None
    assert len(built.media.images) == 1 and len(built.media.files) == 1
    assert len(space.endpoint.parts_of("image_url")) == 1
    assert len(space.endpoint.parts_of("file")) == 1


# ── Outside the allowed places ────────────────────────────────────


async def test_an_absolute_path_outside_the_workspace_is_not_attached(tmp_path: Path):
    space = Workspace(tmp_path, vision=True)
    elsewhere = tmp_path / "elsewhere" / "secret.png"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(PNG)

    built = await space.turn(f"Look at @{elsewhere.resolve()} ")

    assert built.media is None
    assert space.endpoint.parts_of("image_url") == []


async def test_a_symlink_in_the_workspace_to_a_file_outside_is_not_attached(tmp_path: Path):
    space = Workspace(tmp_path, vision=True)
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    link = space.project / "innocent.png"
    link.symlink_to(outside)

    built = await space.turn(f"Look at @{link.resolve().parent / 'innocent.png'} ")

    assert built.media is None


async def test_a_dot_dot_path_out_of_the_uploads_is_not_attached(tmp_path: Path):
    space = Workspace(tmp_path, vision=True)
    (tmp_path / "secret.png").write_bytes(PNG)
    ref = Path(space.upload("decoy.png", PNG))
    escape = ref.parent / ".." / ".." / ".." / ".." / "secret.png"

    built = await space.turn(f"Look at @{escape} ")

    assert built.media is None


def test_a_file_in_the_workspace_outside_the_uploads_is_attachable(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    diagram = (project / "docs" / "diagram.png").resolve()
    diagram.parent.mkdir()
    diagram.write_bytes(PNG)

    resolved = MediaResolver(project_dir=project).resolve_text_references(f"see @{diagram} ")

    assert resolved.attachable == [str(diagram)]
    assert resolved.paths == [], "already absolute: nothing in the text was rewritten"


def test_with_no_project_no_absolute_path_is_attachable(tmp_path: Path):
    image = (tmp_path / "x.png").resolve()
    image.write_bytes(PNG)

    resolved = MediaResolver().resolve_text_references(f"see {image} ")

    assert resolved.attachable == []


@pytest.mark.parametrize("written", ["./shot.png", "shot.png"])
def test_a_path_typed_in_short_is_attachable_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, written: str
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "shot.png").write_bytes(PNG)
    monkeypatch.chdir(project)

    resolved = MediaResolver(project_dir=project, search_dirs=()).resolve_text_references(
        f"see {written} "
    )

    assert resolved.attachable == [str((project / "shot.png").resolve())]
    assert resolved.paths == resolved.attachable
