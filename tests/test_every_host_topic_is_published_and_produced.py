"""Every ``HostTopic`` is written down, and something fires it.

An extension author chooses what to subscribe to from
``docs/EXTENSIBILITY.md``. A topic missing from that page is a topic
nobody asks for however well it works, and a topic on the page that
nothing emits is worse — it is an extension waiting forever for
something that was promised.

Both halves have already gone wrong once. Four topics existed with
only one producer between them, the page read as complete, and the
gap was visible only to whoever remembered that
``extension.state-changed`` was the only thing the registry
happened to already watch.

**Rules, not lists.** Checking the four by name would go green the
moment a fifth arrived. The enum is the source; the page and the
producer module are compared against it, and each failure names the
topic.

The third rule is about *how* a topic is fired. The hook system has
two events fired as string literals rather than through their enum,
which works right up until somebody renames a value and they keep
firing the old name, matching nothing, in silence. There are zero
here and the rule is what keeps it at zero.
"""

from __future__ import annotations

import re
from pathlib import Path

from ember_code.extensions.events import HostTopic

_ROOT = Path(__file__).resolve().parent.parent
_DOC = _ROOT / "docs/EXTENSIBILITY.md"
_PRODUCERS = _ROOT / "src/ember_code/backend/host_events.py"
_SRC = _ROOT / "src/ember_code"
_VOCABULARY = _SRC / "extensions/events.py"


def _events_section() -> str:
    """The page's ``### Events`` section, and only that.

    Scoped rather than whole-page: the permissions, contributions
    and capability tables all have a backticked first cell too, and
    a parser that swept them up would report `secrets` as an
    undelivered promise.
    """
    text = _DOC.read_text()
    start = text.index("### Events")
    end = text.index("###", start + len("### Events"))
    return text[start:end]


def _documented() -> set[str]:
    """Topics named in a ``| `topic` |`` table cell on the page."""
    rows = re.findall(r"^\|\s*`([a-z][a-z0-9.-]*)`\s*\|", _events_section(), re.MULTILINE)
    return set(rows)


class TestBothSidesWereRead:
    """A parser that finds nothing makes every check below vacuous."""

    def test_the_enum_has_members(self):
        assert len(list(HostTopic)) >= 4

    def test_the_page_has_a_topic_table(self):
        assert len(_documented()) >= 4


class TestThePageMatchesTheEnum:
    def test_every_topic_is_on_the_page(self):
        missing = sorted(str(topic) for topic in HostTopic if str(topic) not in _documented())
        assert not missing, (
            f"not in docs/EXTENSIBILITY.md: {missing}. An author picks what "
            "to subscribe to from that page, so a topic missing from it is a "
            "topic nobody asks for."
        )

    def test_the_page_promises_nothing_extra(self):
        known = {str(topic) for topic in HostTopic}
        invented = sorted(topic for topic in _documented() if topic not in known)
        assert not invented, (
            f"documented and not in HostTopic: {invented}. The page is a "
            "promise under EXTENSIBILITY section 8; a row with no member "
            "behind it is one igni cannot keep."
        )


class TestEveryTopicHasAProducer:
    def test_something_emits_each_one(self):
        """A topic nothing fires is an extension waiting forever.

        ``HostEvents`` is the only module allowed to produce, so
        "is there a producer" is answerable by reading one file.
        """
        produced = _PRODUCERS.read_text()
        missing = [topic.name for topic in HostTopic if f"HostTopic.{topic.name}" not in produced]
        assert not missing, (
            f"no producer in host_events.py for: {sorted(missing)}. Publishing "
            "a topic nothing fires is worse than not publishing it — the "
            "extension waits instead of polling."
        )


class TestTopicsTravelAsSymbols:
    def test_nothing_fires_a_bare_string(self):
        """Rename a member and a literal keeps firing the old name.

        It matches nothing, nobody is told, and nothing raises. The
        count is zero; this keeps it there.
        """
        values = {str(topic) for topic in HostTopic}
        offenders: list[str] = []
        for path in sorted(_SRC.rglob("*.py")):
            if path == _VOCABULARY:
                continue  # where the values are defined
            text = path.read_text()
            for value in values:
                if f'"{value}"' in text or f"'{value}'" in text:
                    offenders.append(f"{path.relative_to(_ROOT)}: {value}")
        assert not offenders, (
            f"topics written as string literals: {offenders}. Use the "
            "HostTopic member — a renamed value then fails at import rather "
            "than delivering to nobody."
        )
