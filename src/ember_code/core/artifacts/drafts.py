"""The draft of a document nobody has shared.

A shared document keeps its draft on the server, where everybody who
can read the document can read what is being written into it. A
document that was never shared has nobody to share a draft with — and
still needs one, because the thing a draft buys is the same either
way: an edit is somewhere other than an unsaved editor, and a version
is made deliberately rather than by typing.

So the same two acts, with a file standing in for the server. One
file per artifact, holding the text and the version it was written
from. It is not a copy of the version history and never grows into
one: publishing turns it into a version and deletes it, which is the
only way a draft leaves this directory.

Deliberately not in the graph. A draft is not a fact about the
document — it is one person's half-finished sentence, with no
lineage, no chunks and nothing to retrieve it by, and putting it in
the store that answers "what do we know" would make it findable as
though it were an answer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where drafts live under the igni root.
DRAFTS_DIR = "drafts"


@dataclass(frozen=True)
class LocalDraft:
    """An unpublished edit held on this machine."""

    content: str
    #: Which version it was written from. Behind the current one means
    #: something was published underneath it.
    base_n: int


class LocalDrafts:
    """Drafts of documents that live only here.

    Args:
        data_dir: the igni root, usually ``~/.igni``.
        project_id: which project's artifacts these belong to. Two
            projects can hold artifacts with the same id — the id is
            derived from the name — and one project's draft appearing
            in another would be somebody else's sentence in your
            document.
    """

    def __init__(self, *, data_dir: str | Path, project_id: str) -> None:
        self._dir = Path(data_dir).expanduser() / DRAFTS_DIR / project_id

    def _path(self, artifact_id: str) -> Path:
        # The id, and nothing taken from a name. Ids are minted here
        # and are safe in a path; a title is typed by somebody and can
        # contain a slash, which is a way of writing outside this
        # directory.
        safe = "".join(c for c in artifact_id if c.isalnum() or c in "-_")
        return self._dir / f"{safe}.json"

    def read(self, *, artifact_id: str) -> LocalDraft | None:
        """What is being written on this document, if anything."""
        path = self._path(artifact_id)
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            # Unreadable is reported as absent rather than raised. The
            # document itself is intact in the graph, and a draft this
            # process cannot parse must not stand between somebody and
            # the document they asked for.
            logger.info("could not read the draft at %s: %s", path, exc)
            return None
        return LocalDraft(
            content=str(body.get("content") or ""),
            base_n=int(body.get("base_n") or 0),
        )

    def write(self, *, artifact_id: str, content: str, base_n: int) -> None:
        """Keep an edit without making a version.

        Written to a neighbouring file and moved into place, so a
        process that dies mid-write leaves the previous draft rather
        than half of this one. A truncated draft is worse than a stale
        one: the stale draft is something somebody wrote.
        """
        path = self._path(artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        beside = path.with_suffix(".json.part")
        beside.write_text(
            json.dumps({"content": content, "base_n": int(base_n)}),
            encoding="utf-8",
        )
        beside.replace(path)

    def clear(self, *, artifact_id: str) -> None:
        """Forget it — because it became a version, and only then."""
        self._path(artifact_id).unlink(missing_ok=True)
