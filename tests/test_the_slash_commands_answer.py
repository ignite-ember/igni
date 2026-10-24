"""Slash commands that answered wrongly, found by running them.

Every one was reachable from the composer's menu, and none had a test.
They were found the only way this kind of thing is found — by typing
each of the 31 commands into a live app and reading what came back.

* ``/config`` raised. ``ConfigView`` read ``settings.storage.backend``,
  a field ``StorageConfig`` no longer has — choosing a storage backend
  went away with the backward-compat cleanup and the renderer did not.
  The user saw ``session routing failed: 'StorageConfig' object has no
  attribute 'backend'``. Same shape as the ``/ctx`` defect: an
  attribute error from a view model, surfaced through the message
  dispatcher's generic catch as a routing failure.

* ``/plugin`` did nothing at all — no output, no panel, not even the
  echoed command line. That one is in ``Composer.test.ts``; the
  composer highlighted ``/plugins`` when you typed ``/plugin``, and
  Enter compared the typed text against the highlighted entry.

The pattern: a command whose *output* nobody had read. ``/config``
failed in a way that looked like infrastructure ("routing failed")
rather than like the small code defect it was, which is why it
survived — the error text pointed away from the file with the bug.
"""

from __future__ import annotations

import pytest

from ember_code.backend.schemas_config import ConfigView
from ember_code.core.config.schemas.storage_and_paths import StorageConfig


class _Settings:
    """The slice of ``Session.settings`` that ``ConfigView`` reads."""

    class models:
        default = "MiniMax-M2.7"

    class permissions:
        file_write = "ask"
        shell_execute = "ask"

    storage = StorageConfig()

    class learning:
        enabled = True

    class reasoning:
        enabled = False

    class guardrails:
        pii_detection = True
        prompt_injection = False
        moderation = False

    class artifacts:
        enabled = True

    class orchestration:
        max_total_agents = 8
        max_nesting_depth = 3


class _Session:
    settings = _Settings()
    session_id = "abc123"


class _NoCreds:
    """A credentials store that finds nothing — the offline case."""

    def load(self):
        class _Result:
            ok = False
            creds = None

        return _Result()


class TestConfigRenders:
    def test_from_session_does_not_raise(self):
        """The regression. This threw ``AttributeError`` for every
        user, every time, and the message they saw named neither the
        command nor the field."""
        view = ConfigView.from_session(_Session(), store=_NoCreds())

        assert view.model == "MiniMax-M2.7"

    def test_it_reads_a_field_storage_config_actually_has(self):
        """Stated against the schema rather than against the view.

        The defect was a reference to a field that had been deleted, so
        the check that matters is that whatever ``/config`` reads is
        still in ``StorageConfig``. A test that only called
        ``from_session`` would go green again the moment someone
        reintroduced a differently-broken attribute.
        """
        assert "data_dir" in StorageConfig.model_fields
        assert "backend" not in StorageConfig.model_fields

    def test_the_card_names_where_data_lives(self):
        """Against the session's own settings, not a fresh ``StorageConfig()``.

        The default is resolved per instance — it reads the filesystem to
        decide between the pre- and post-rename home (see
        ``storage_and_paths._home_config_path``) — so a config built inside the
        test and one built when this module was imported can legitimately name
        different directories. Comparing the card to a fresh default therefore
        tests the clock, not the card. What the card owes the user is the
        directory *this session* is using.
        """
        session = _Session()
        text = ConfigView.from_session(session, store=_NoCreds()).to_command_result().content

        assert "**Storage:**" in text
        assert session.settings.storage.data_dir in text

    def test_the_card_is_not_an_error(self):
        """How the user met it: a red line beginning "session routing
        failed". Asserted on the rendered text because that is what
        reached them."""
        text = ConfigView.from_session(_Session(), store=_NoCreds()).to_command_result().content

        assert text.startswith("## Configuration")
        assert "routing failed" not in text
        assert "has no attribute" not in text

    @pytest.mark.parametrize(
        "field",
        ["Model", "Auth", "Permissions", "Storage", "Session"],
    )
    def test_every_headline_field_is_present(self, field: str):
        """A renderer that silently drops a line is the failure this
        class cannot otherwise see: ``from_session`` succeeding says
        nothing about the card being complete."""
        text = ConfigView.from_session(_Session(), store=_NoCreds()).to_command_result().content

        assert f"**{field}:**" in text
