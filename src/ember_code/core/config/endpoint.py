"""Where the server is, and the refusal when nobody has said.

DEC-14, and DP-5a in ember-server's ``docs/COMPLIANCE_TRACKER.md``.

``settings.api_url`` defaulted to the vendor's own API host. For a
product whose whole claim is that nothing leaves the customer's cloud,
a vendor host baked into the client is the wrong default in the most
load-bearing place there is: it is where the CLI sends the sign-in code
it redeems, the models it lists, and the changesets it syncs.

The decision was already made — *"there is to be no vendor endpoint"* —
and the change was not, because flipping the default to empty naively
produces requests to ``/v1/portal/me``, a relative URL that resolves to
nothing and fails as a malformed-URL error somewhere deep in httpx. That
is worse than the wrong default: an operator who has not configured a
server gets a stack trace about URL parsing rather than a sentence
telling them what to set.

So the default is empty **and** every path that needs it goes through
:func:`require_api_url`, which refuses with the setting's name, the file
it lives in and an example. The F69 pattern: a misconfiguration fails
loudly and immediately instead of doing something silently wrong.

**The plan this anticipates.** The stated future is that a customer sets
this to their own cloud, and igni's own routing disappears — at which
point there is no vendor to default to and this module is simply correct
rather than strict.

**What this is not.** ``model_entry`` classifies a *configured* URL by
whether its host is ``ignite-ember.sh``, to decide whether a model is
vendor-routed or user-managed. That is a question about a value somebody
chose, not a default, and it is deliberately left alone.

**Why no vendor URL appears in this file.**
``tests/test_no_vendor_endpoint_is_assumed.py`` fails on any module
containing one, and it reads this module too — so the paragraphs above
describe the removed defaults instead of quoting them. A rule that let
an explanation of a bad default quote the bad default would let anything
quote it.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

#: Named so the message can say it, and so a grep for the setting finds
#: the place that refuses without it.
SETTING = "api_url"
CONFIG_FILE = "~/.igni/config.yaml"


class ApiUrlNotConfigured(RuntimeError):
    """Raised when an operation needs the server and none is set.

    A distinct type rather than ``ValueError`` so a caller that wants to
    degrade — offer local models, skip a sync — can catch exactly this
    and not swallow a genuine URL problem alongside it.
    """


def _message(operation: str) -> str:
    return (
        f"{operation} needs to know where your igni server is, and `{SETTING}` is not set.\n"
        f"\n"
        f"Set it in {CONFIG_FILE}:\n"
        f"\n"
        f"    {SETTING}: https://igni.your-company.example\n"
        f"\n"
        f"There is deliberately no default. igni is self-hosted: pointing the client at a "
        f"vendor host by default would send your sign-in code, your model list and your "
        f"changesets somewhere you did not choose."
    )


def require_api_url(value: str | None, *, operation: str) -> str:
    """``value`` with no trailing slash, or refuse saying what to set.

    ``operation`` is a short phrase naming what the caller was trying to
    do — "Signing in", "Listing models". It leads the message, because
    "api_url is not set" leaves the reader to work out which of the
    things they just asked for needed it.
    """
    if value is None or not value.strip():
        raise ApiUrlNotConfigured(_message(operation))
    return value.strip().rstrip("/")


def is_configured(value: str | None) -> bool:
    """Whether an operation that *can* degrade should try at all.

    For the paths where not having a server is a supported state rather
    than an error — local-only models, no CodeIndex — so they can skip
    quietly instead of catching :class:`ApiUrlNotConfigured` and
    discarding it, which reads as swallowing a real failure.
    """
    return bool(value and value.strip())


def portal_url_for(api_url: str) -> str:
    """The human-facing portal that pairs with ``api_url``.

    Derived rather than configured, and this is the second half of
    DEC-14: ``PortalClient`` took ``portal_url`` with a default of
    the vendor's website and **nothing could change it** — there was no
    setting for it at all. So a customer who correctly pointed
    ``api_url`` at their own deployment still had the CLI open the
    vendor's website to sign in. One wrong default was configurable; the
    other was not, which made it the worse of the two.

    The rule is the one ``CodeIndexInstallHint`` already used to build a
    repository link, lifted here so there is one implementation instead
    of two that can disagree: drop a leading ``api`` label, or an
    ``api-``/``-api`` affix on the first label. ``api.example.com`` →
    ``example.com``; ``dev-api.example.com`` → ``dev.example.com``. A
    host that does not look like an API host is returned unchanged,
    because a deployment serving both from one name is a normal shape and
    guessing at it would be worse than doing nothing.
    """
    parsed = urlparse(require_api_url(api_url, operation="Opening the portal"))
    host = parsed.netloc
    first, sep, rest = host.partition(".")
    if first == "api":
        new_host = rest or host
    elif first.endswith("-api"):
        new_host = f"{first[: -len('-api')]}{sep}{rest}"
    elif first.startswith("api-"):
        new_host = f"{first[len('api-') :]}{sep}{rest}"
    else:
        new_host = host
    return urlunparse((parsed.scheme or "https", new_host, "", "", "", "")).rstrip("/")
