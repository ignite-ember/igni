"""DEC-14: nothing may default to a host the vendor runs.

`api_url` defaulted to ``https://api.ignite-ember.sh``. For a product
whose whole claim is that nothing leaves the customer's cloud, that is
the wrong default in the most load-bearing place there is — it is where
the CLI sends the sign-in code it redeems, the models it lists and the
changesets it syncs.

The decision had been made for a long time (*"there is to be no vendor
endpoint"*) and the change had not, because flipping the default to
empty naively produces relative URLs that fail as parse errors deep in
httpx. `tests/test_nothing_phones_home.py` recorded that in as many
words: its allow-list entry for `api.ignite-ember.sh` began "OPEN
QUESTION, not a settled answer" and explained what a proper fix needed.

So there are two halves, and this file is about both.

**No vendor default anywhere.** Not just `api_url`: `PortalClient` had a
second one, ``https://ignite-ember.sh`` for the portal host, and that
was the worse of the two — there was **no setting for it at all**, so a
customer who correctly pointed the CLI at their own deployment still had
it open the vendor's website to sign in, with no way to discover or fix
that. It is derived from `api_url` now. The tools' `cloud_server_url`
had one too.

**And a refusal, not a malformed request.** Every path that needs the
server either refuses saying what to set, or — where having no server is
a supported state — skips with a reason that names the setting. The
difference matters: "not signed in to igni" sends somebody to a login
flow that has nothing to log in to.

The scan below is the part that will still be true in a year. Naming the
five call sites that had a default is a snapshot; asserting that no
module under `src/ember_code` contains a vendor URL literal is the rule,
and it fails on the sixth.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from ember_code.core.config.endpoint import (
    ApiUrlNotConfigured,
    is_configured,
    portal_url_for,
    require_api_url,
)
from ember_code.core.config.settings import Settings

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ember_code"

#: A URL whose host is the vendor's. Matched as a URL rather than as a
#: bare hostname on purpose: `model_entry.py` classifies a *configured*
#: URL by whether its host is `ignite-ember.sh`, to decide whether a
#: model is vendor-routed or user-managed. That is a question about a
#: value somebody chose, not a default, and flagging it would be the
#: rule crying wolf.
_VENDOR_URL = re.compile(r"https?://(?:[\w-]+\.)*ignite-ember\.sh")

#: The documentation host is not an endpoint. It appears in a generated
#: config template as a link for a human to click, contacts nothing, and
#: is separately allow-listed in `test_nothing_phones_home.py`.
_DOCS = "docs.ignite-ember.sh"


def _modules() -> list[pathlib.Path]:
    return sorted(_SRC.rglob("*.py"))


class TestTheScanMeasuresSomething:
    def test_there_are_modules_to_scan(self):
        assert len(_modules()) > 100, len(_modules())

    def test_the_pattern_matches_what_it_is_for(self):
        """A regex that matches nothing passes every file."""
        assert _VENDOR_URL.search('api_url: str = "https://api.ignite-ember.sh"')
        assert _VENDOR_URL.search('"https://ignite-ember.sh"')
        assert _VENDOR_URL.search('"https://dev-api.ignite-ember.sh/v1"')

    def test_the_pattern_leaves_the_classification_alone(self):
        """`model_entry` asks whether a URL a user configured points at
        the vendor. That is not a default, and a rule that could not
        tell the difference would have to be deleted or ignored."""
        assert not _VENDOR_URL.search('return "ignite-ember.sh" in self.url')
        assert not _VENDOR_URL.search('CLOUD_GATEWAY_HOST = "api.ignite-ember.sh"')


class TestNoVendorEndpointIsBakedIn:
    def test_no_module_contains_a_vendor_url(self):
        offenders: dict[str, list[str]] = {}
        for module in _modules():
            hits = [h for h in _VENDOR_URL.findall(module.read_text()) if _DOCS not in h]
            if hits:
                offenders[str(module.relative_to(_SRC))] = sorted(set(hits))

        assert not offenders, (
            f"these modules name a vendor URL: {offenders}. igni is self-hosted — a URL "
            "compiled in here is where an unconfigured install sends its sign-in code, its "
            "model list or its changesets. Read it from `api_url` and refuse when unset "
            "(core/config/endpoint.py)."
        )

    def test_the_setting_itself_has_no_default(self):
        assert Settings().api_url == ""

    def test_the_updater_endpoint_has_no_default_either(self):
        """DEC-11's half of the same question. Empty means the endpoint
        compiled into `tauri.conf.json`, which is deliberate and
        documented — but the *setting* must not carry a second copy of
        it, or the two can disagree."""
        assert Settings().update_endpoint == ""


class TestTheRefusalSaysWhatToDo:
    def test_it_names_the_setting_the_file_and_an_example(self):
        with pytest.raises(ApiUrlNotConfigured) as raised:
            require_api_url("", operation="Signing in")

        message = str(raised.value)
        assert "api_url" in message
        assert "~/.igni/config.yaml" in message
        assert "https://igni.your-company.example" in message

    def test_it_leads_with_the_operation(self):
        """ "api_url is not set" leaves the reader to work out which of
        the things they asked for needed it."""
        with pytest.raises(ApiUrlNotConfigured) as raised:
            require_api_url(None, operation="Listing models")

        assert str(raised.value).startswith("Listing models")

    def test_it_says_why_there_is_no_default(self):
        """Without the reason this reads as an oversight, and the
        obvious fix somebody reaches for is to put a default back."""
        with pytest.raises(ApiUrlNotConfigured) as raised:
            require_api_url("", operation="Signing in")

        assert "self-hosted" in str(raised.value)

    @pytest.mark.parametrize("blank", ["", "   ", "\t", None])
    def test_whitespace_is_not_a_configuration(self, blank):
        with pytest.raises(ApiUrlNotConfigured):
            require_api_url(blank, operation="Signing in")
        assert is_configured(blank) is False

    def test_a_configured_url_comes_back_normalised(self):
        assert require_api_url("https://a.example/", operation="x") == "https://a.example"
        assert require_api_url("  https://a.example  ", operation="x") == "https://a.example"
        assert is_configured("https://a.example") is True

    def test_the_exception_is_its_own_type(self):
        """So a caller that wants to degrade can catch exactly this and
        not swallow a genuine URL problem alongside it."""
        assert issubclass(ApiUrlNotConfigured, RuntimeError)
        assert not issubclass(ApiUrlNotConfigured, ValueError)


class TestThePortalHostIsDerived:
    @pytest.mark.parametrize(
        "api_url,expected",
        [
            ("https://api.acme.example", "https://acme.example"),
            ("https://dev-api.acme.example", "https://dev.acme.example"),
            ("https://api-dev.acme.example", "https://dev.acme.example"),
            # Not an API host: one deployment serving both from one name
            # is a normal shape, and guessing would be worse than doing
            # nothing.
            ("https://igni.acme.example", "https://igni.acme.example"),
            # Trailing slash and path are dropped — this is a host, not
            # an endpoint.
            ("https://api.acme.example/v1/", "https://acme.example"),
            # Scheme is preserved rather than forced, so a customer on
            # an internal http host is not silently redirected.
            ("http://api.internal", "http://api.internal".replace("api.", "")),
        ],
    )
    def test_the_rules(self, api_url, expected):
        assert portal_url_for(api_url) == expected

    def test_it_refuses_when_there_is_no_api_url(self):
        with pytest.raises(ApiUrlNotConfigured):
            portal_url_for("")

    def test_the_install_hint_uses_the_same_rule(self):
        """The rules lived here and in `CodeIndexInstallResult`, which
        builds a "link your repository" URL. Two copies of a
        hostname-guessing rule is one too many — a customer following
        one link and not the other is a support ticket nobody can
        reproduce."""
        from ember_code.backend.schemas_codeindex_rpc import CodeIndexInstallResult

        hint = CodeIndexInstallResult.from_api_url("https://dev-api.acme.example")

        assert hint.install_url == "https://dev.acme.example/repositories"
        assert hint.install_url.startswith(portal_url_for("https://dev-api.acme.example"))


class TestPathsThatDegradeRatherThanRefuse:
    """Not having a server is a supported state for some things.

    A first run with no `api_url` is not broken — it has local models
    and no CodeIndex. Those paths must skip with a reason that names the
    setting, rather than raise, and must not report the wrong reason.
    """

    def test_the_model_catalogue_reports_no_server_not_no_token(self):
        """Distinct reasons because the two ask different things of the
        user: one means "sign in", the other means "tell me where your
        server is". Collapsing them sends somebody to a login flow with
        nothing to log in to."""
        from ember_code.core.config.cloud_models import (
            CloudModelCatalogClient,
            FetchReason,
        )

        result = CloudModelCatalogClient("", cloud_token="a-real-looking-token").fetch()

        assert result.ok is False
        assert result.reason is FetchReason.NO_SERVER

    def test_it_is_checked_before_the_token(self):
        """You cannot be signed in to a server you have not named, so
        the server check has to come first or the reason is wrong for
        the common case of a fresh install."""
        from ember_code.core.config.cloud_models import (
            CloudModelCatalogClient,
            FetchReason,
        )

        result = CloudModelCatalogClient("", cloud_token=None).fetch()

        assert result.reason is FetchReason.NO_SERVER

    def test_a_sync_with_no_server_says_which_setting(self):
        from ember_code.core.code_index.sync.schemas import SyncResult

        result = SyncResult.no_server_configured()

        assert result.skipped is True
        assert "api_url" in result.reason
        assert "config.yaml" in result.reason

    def test_that_reason_is_distinct_from_not_being_signed_in(self):
        from ember_code.core.code_index.sync.schemas import SyncResult

        assert SyncResult.no_server_configured().reason != SyncResult.not_authenticated().reason
