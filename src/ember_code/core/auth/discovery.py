"""Find the igni server from the address somebody types.

Asking a person for a server URL asks them for something they usually
do not know. They know their email address, and the domain in it is
already the name of the organisation whose igni they should reach —
so the domain answers the question, the way it answers it for mail.

**The record lives in the customer's own zone.** ``acme.example``
publishes where its igni is; this reads it. Nothing vendor-run is
contacted, which is the whole point: a lookup service in the middle
would learn every user's address before they had chosen anything, on
a product whose claim is that nothing leaves the customer's
infrastructure. It would also put a third party in the path of every
login, including the air-gapped ones.

What a customer publishes, once::

    https://acme.example/.well-known/igni.json
    {"api_url": "https://igni.acme.example"}

Whoever controls the domain decides where its people sign in. That is
the same trust the address itself carries, so it is the right party —
but it also means the fetch has to be exactly as careful as any other
request to a host we were told about rather than chose:

* **https only.** Plaintext would let anybody between here and there
  name the server that receives sign-ins for the whole company.
* **Redirects are not followed.** A redirect is a different host
  answering a question asked of this one.
* **Bounded.** A short timeout and a size cap, because a discovery
  that hangs is a sign-in screen that hangs.
* **The answer is checked, not trusted.** An ``api_url`` that is not
  an https URL is refused rather than written into a config file.

Discovery failing is normal and is not an error to shout about: most
domains have no record, and the screen offers the address field
instead. It only *fails* in the sense that the next thing to try is
asking.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

#: Where a customer publishes it, relative to their email domain.
WELL_KNOWN = "/.well-known/igni.json"

#: Long enough for a real server, short enough that a domain which
#: simply does not answer does not hold the sign-in screen.
TIMEOUT_SECONDS = 4.0

#: A generous ceiling for a document with two keys in it. Stops a
#: discovery fetch being a way to stream a gigabyte into the client.
MAX_BYTES = 64 * 1024

#: Hosts allowed to speak plaintext, for developing against a local
#: deployment. Nothing else: see the module docstring.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


@dataclass(frozen=True)
class Discovered:
    """What the domain said, or why it said nothing usable."""

    api_url: str = ""
    #: Where a person signs in. Empty means "derive it from
    #: ``api_url``", which is right for a deployment serving both
    #: under one name.
    portal_url: str = ""
    #: How it was found, for the screen to say so. Empty when nothing
    #: was found.
    source: str = ""
    #: Why there is no answer. Shown only when somebody asks; the
    #: normal case is a domain with no record, which is not a fault.
    reason: str = ""

    @property
    def found(self) -> bool:
        return bool(self.api_url)


def domain_of(email: str) -> str:
    """The domain part, lowercased, or empty if this is not an address.

    Deliberately strict about shape. The value is interpolated into a
    URL, so ``sam@acme.example/../../evil`` must not become a request
    to somewhere else — a domain with a slash, a space or no dot in it
    is not a domain and is refused here rather than sanitised into
    something plausible.
    """
    address = (email or "").strip().lower()
    local, sep, domain = address.rpartition("@")
    # ``local`` matters: "@acme.example" has a domain and is not an
    # address, and treating it as one would send a lookup on behalf of
    # a person who typed nothing.
    if not sep or not domain or not local:
        return ""
    if any(c in domain for c in "/\\?#@: \t") or ".." in domain:
        return ""
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return ""
    return domain


def is_safe_url(url: str) -> bool:
    """Whether a URL is one we are willing to write down as the server.

    Public because the same question is asked of an address somebody
    typed by hand, and the answer must not differ by how it arrived:
    a plaintext host decides where sign-in codes are sent either way.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not parsed.netloc:
        return False
    if parsed.scheme == "https":
        return True
    # http only for a local deployment somebody is developing against.
    host = parsed.hostname or ""
    return parsed.scheme == "http" and host in LOCAL_HOSTS


def _read(payload: bytes) -> Discovered:
    """Turn the published document into an answer, or a reason."""
    try:
        document = json.loads(payload)
    except ValueError:
        return Discovered(reason="The record there is not valid JSON.")
    if not isinstance(document, dict):
        return Discovered(reason="The record there is not an object.")

    api_url = str(document.get("api_url") or "").strip().rstrip("/")
    portal_url = str(document.get("portal_url") or "").strip().rstrip("/")
    if not api_url:
        return Discovered(reason="The record there does not name an api_url.")
    if not is_safe_url(api_url):
        return Discovered(reason=f"The record there names {api_url!r}, which is not an https URL.")
    if portal_url and not is_safe_url(portal_url):
        # The api_url is usable on its own and the portal is derived
        # when absent, so one bad field does not sink the lookup.
        logger.info("discovery: ignoring unsafe portal_url %r", portal_url)
        portal_url = ""
    return Discovered(api_url=api_url, portal_url=portal_url, source="well-known")


async def discover(email: str) -> Discovered:
    """Where this address's organisation runs igni.

    Never raises. Every outcome is a :class:`Discovered` — found, or
    carrying a reason the screen can show if somebody asks why the
    address field appeared.
    """
    domain = domain_of(email)
    if not domain:
        return Discovered(reason="That does not look like an email address.")

    url = f"https://{domain}{WELL_KNOWN}"
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS,
            # A redirect is a different host answering a question
            # asked of this one.
            follow_redirects=False,
        ) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001 — DNS, TLS, timeouts, refusals
        logger.debug("discovery: %s did not answer: %r", url, exc)
        return Discovered(reason=f"{domain} did not answer.")

    if response.status_code != 200:
        return Discovered(reason=f"{domain} has no igni record published.")
    if len(response.content) > MAX_BYTES:
        return Discovered(reason=f"The record at {domain} is too large to be one.")
    return _read(response.content)
