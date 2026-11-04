"""The header can name the account, not just the organisation.

The chip in igni's top right rendered ``cloud_org`` and nothing else,
so an account with no organisation claim showed a bare cloud icon —
signed in, and unable to say as whom.

The information was never missing. ``/portal/me`` returns ``email``
and ``full_name``; the client's view of that response deliberately
trimmed to email + tier + org, and the payload the popover fetches
then dropped the email too. Three layers each discarding a little
more, none of them wrong on its own.

The address also needs no network at all — it is in the credentials
file from the moment somebody logs in, which is what lets the header
name the account on the first status push rather than a round trip
later. A header that fills in a second after the window opens reads
as a glitch.
"""

from __future__ import annotations

import json

from ember_code.backend.schemas_rpc import CloudPlan
from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.auth.schemas import UserInfo
from ember_code.core.session.cloud_auth import SessionCloudAuth


def _creds(tmp_path, **fields) -> CloudCredentials:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"access_token": "t", "token_type": "bearer", **fields}))
    return CloudCredentials(str(path))


class TestTheAddressNeedsNoNetwork:
    def test_it_comes_straight_off_the_credentials_file(self, tmp_path):
        auth = SessionCloudAuth(
            creds=_creds(tmp_path, email="maya@acme.test"),
            server_url="https://igni.example",
            rebuild_team=lambda: None,
            catalog=None,  # type: ignore[arg-type]
        )
        assert auth.email == "maya@acme.test"

    def test_a_logged_out_session_has_none(self, tmp_path):
        auth = SessionCloudAuth(
            creds=CloudCredentials.empty(),
            server_url="",
            rebuild_team=lambda: None,
            catalog=None,  # type: ignore[arg-type]
        )
        assert not auth.email


class TestTheNameSurvivesTheWireMapping:
    def test_user_info_keeps_the_display_name(self):
        """The portal has returned this all along and the client's
        model dropped it, which is why the header could only ever show
        an organisation."""
        info = UserInfo.model_validate(
            {"email": "maya@acme.test", "full_name": "Maya Okonkwo", "tier": "pro"}
        )
        assert info.full_name == "Maya Okonkwo"

    def test_the_popover_payload_carries_both(self):
        plan = CloudPlan.from_user_info(
            UserInfo(email="maya@acme.test", full_name="Maya Okonkwo", tier="pro")
        )
        assert (plan.email, plan.full_name) == ("maya@acme.test", "Maya Okonkwo")

    def test_it_still_carries_the_plan_it_is_named_for(self):
        plan = CloudPlan.from_user_info(
            UserInfo(email="a@b.test", tier="pro", org_display_name="Acme")
        )
        assert (plan.tier, plan.org_name) == ("pro", "Acme")

    def test_a_response_with_no_name_does_not_invent_one(self):
        # An account that never set a display name falls back to its
        # address, which the header does — but the payload must not
        # fabricate something for it to fall back *from*.
        plan = CloudPlan.from_user_info(UserInfo(email="a@b.test"))
        assert plan.full_name is None

    def test_extra_portal_fields_are_ignored_rather_than_fatal(self):
        # The portal is free to add fields without breaking the client.
        info = UserInfo.model_validate(
            {"email": "a@b.test", "full_name": "A B", "something_new": 1}
        )
        assert info.full_name == "A B"
