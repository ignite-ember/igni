"""Every ``HookEvent`` must be on the page that tells people it exists.

Hooks are the product's extension surface. A self-hosted deployment
customises igni by writing them, and ``docs/HOOKS.md`` is the only
place that says which events there are — so an event missing from that
page is an event nobody can use, however well it works.

Eight of the eighteen were missing when this was written:
``PermissionRequest``, ``PermissionDenied``, ``PreCompact``,
``PostCompact``, ``TaskCreated``, ``TaskCompleted``, ``StopFailure``
and ``InstructionsLoaded``. Every one of them fires in the product.
The page listed ten and read as complete.

**A rule, not a list.** Checking for those eight by name would go
green the moment a nineteenth arrived. The enum is the source, the
document is compared against it, and the failure names the event.

The second rule here is about *how* events are fired. Two of them —
``SubagentStart`` and ``SubagentStop`` — are fired with string
literals rather than through :class:`HookEvent`. That works today,
which is the problem: rename a value in the enum and those two keep
firing the old name, matching nothing, silently. The rule counts the
bare-string call sites so the number cannot grow.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ember_code.core.hooks.events import HookEvent

_ROOT = Path(__file__).resolve().parent.parent
_DOC = _ROOT / "docs/HOOKS.md"
_SRC = _ROOT / "src/ember_code"


def _events() -> list[str]:
    return [e.value for e in HookEvent]


class TestBothSidesWereRead:
    """A parser that finds nothing makes every check below vacuous."""

    def test_the_enum_has_events(self):
        assert len(_events()) >= 15

    def test_the_document_exists_and_is_substantial(self):
        assert _DOC.exists()
        assert len(_DOC.read_text()) > 2000


class TestTheDocumentCoversTheEnum:
    @pytest.mark.parametrize("event", _events())
    def test_the_event_has_a_row_of_its_own(self, event: str):
        """A **table row**, not a mention.

        The first version of this asked whether the name appeared
        anywhere on the page. Deleting the entire permission-events
        section left it green — because the paragraph I had written
        underneath, explaining that eight events had been missing,
        names all eight in backticks. A rule satisfied by its own
        changelog is not a rule.

        A row is what a reader actually needs: the event, when it
        fires, whether it can block, and what it carries.
        """
        rows = [
            ln
            for ln in _DOC.read_text().splitlines()
            if ln.startswith("|") and f"`{event}`" in ln.split("|")[1]
        ]

        assert rows, (
            f"{event} fires in the product and has no row in docs/HOOKS.md's "
            "event tables. An event nobody can find is an event nobody can hook."
        )
        # Four columns, all filled. A row with an empty "when it fires"
        # cell passes the existence check and tells the reader nothing.
        cells = [c.strip() for c in rows[0].strip().strip("|").split("|")]
        assert len(cells) >= 3, f"{event}'s row is malformed: {rows[0]!r}"
        assert all(cells[:3]), f"{event}'s row has an empty cell: {rows[0]!r}"

    def test_no_row_invents_an_event(self):
        """The rule the other way.

        A page naming an event that does not exist sends someone off to
        write a hook that will never run — worse than an omission,
        because it fails silently at their end rather than at ours.
        """
        doc = _DOC.read_text()
        # Backticked CamelCase words in the events section only: the
        # rest of the page is full of tool names, JSON keys and shell.
        section = doc[doc.index("## Hook Events") : doc.index("## Configuration")]
        named = set(re.findall(r"`([A-Z][A-Za-z]+)`", section))
        known = set(_events())
        # Words that are legitimately capitalised and not events.
        allowed = {"ASK", "DENY", "RulesIndex", "HookEvent", "Slack"}

        assert not (named - known - allowed), (
            f"{sorted(named - known - allowed)} appear in the Hook Events section "
            "and are not in HookEvent."
        )


class TestEventsAreFiredThroughTheEnum:
    """Bare strings at a fire site outlive a rename; the enum does not."""

    #: ``orchestrate_spawn._fire_start`` / ``_fire_stop_once``. Both
    #: are known and both are wrong in the same way; the point of the
    #: number is that it cannot grow.
    _KNOWN_BARE_STRING_FIRES = 2

    def _fire_sites(self) -> list[str]:
        """Lines calling a hook executor with a literal event name."""
        hits: list[str] = []
        for path in _SRC.rglob("*.py"):
            for line in path.read_text().splitlines():
                if re.search(r'_fire_hook\(\s*"[A-Za-z]+"', line) or re.search(
                    r'execute\(\s*event\s*=\s*"[A-Za-z]+"', line
                ):
                    hits.append(f"{path.relative_to(_ROOT)}: {line.strip()}")
        return hits

    def test_bare_string_fire_sites_do_not_multiply(self):
        sites = self._fire_sites()

        assert len(sites) <= self._KNOWN_BARE_STRING_FIRES, (
            "a hook event is being fired by string literal rather than through "
            "HookEvent, so a rename in the enum would silently stop matching it:\n"
            + "\n".join(sites)
        )

    def test_every_bare_string_names_a_real_event(self):
        """Until they are converted, at least make them wrong loudly."""
        known = set(_events())
        for site in self._fire_sites():
            name = re.search(r'"([A-Za-z]+)"', site)
            assert name and name.group(1) in known, (
                f"{site} fires an event name that is not in HookEvent at all."
            )


class TestTheEventsThatFire:
    """Which events the product actually raises, as a fact on record.

    ``Notification`` is in the enum, on the page, and fired from
    nowhere. The page says so — "reserved for future use" — and this
    pins that the two statements agree. If somebody wires it up, this
    test is where they find out to update the page.
    """

    def _enum_fire_sites(self) -> set[str]:
        fired: set[str] = set()
        for path in _SRC.rglob("*.py"):
            if path.name == "events.py":
                continue
            text = path.read_text()
            for member in re.findall(r"HookEvent\.([A-Z_0-9]+)", text):
                with_value = getattr(HookEvent, member, None)
                if with_value is not None:
                    fired.add(with_value.value)
            for literal in re.findall(r'_fire_hook\(\s*"([A-Za-z]+)"', text):
                fired.add(literal)
        return fired

    def test_notification_is_fired_nowhere_and_the_page_says_so(self):
        assert "Notification" not in self._enum_fire_sites()
        section = _DOC.read_text()
        row = next(ln for ln in section.splitlines() if "`Notification`" in ln)

        assert "reserved" in row.lower(), (
            "Notification is not fired anywhere; the page must not imply it is."
        )

    def test_every_other_event_is_fired_somewhere(self):
        fired = self._enum_fire_sites()
        never = sorted(set(_events()) - fired - {"Notification"})

        assert never == [], (
            f"{never} are declared, documented, and raised by nothing. Either "
            "wire them up or mark them reserved like Notification."
        )
