"""Authentication module — OTP login, token storage, and credential management.

Public surface:

* :class:`~ember_code.core.auth.portal_client.PortalClient` +
  :class:`~ember_code.core.auth.callback_server.CallbackServer`
  drive the browser-callback CLI-auth flow.
* :class:`~ember_code.core.auth.schemas.UserInfo`,
  :class:`~ember_code.core.auth.schemas.LoginResult`,
  :class:`~ember_code.core.auth.schemas.ValidateResult`,
  :class:`~ember_code.core.auth.schemas.LoadCredentialsResult`, and
  :class:`~ember_code.core.auth.schemas.JwtClaims` are the Pydantic
  result shapes callers unpack.
* :class:`CloudCredentials` / :class:`Credentials` +
  :class:`CredentialsStore` cover on-disk storage — everything
  used to be a free function is now a method on the coordinator
  class (:meth:`CredentialsStore.save` / :meth:`.load` / :meth:`.clear`,
  :meth:`Credentials.new` / :meth:`Credentials.is_expired`,
  :meth:`JwtClaims.decode`).
"""

from ember_code.core.auth.callback_server import CallbackServer
from ember_code.core.auth.credentials import (
    CloudCredentials,
    Credentials,
    CredentialsStore,
    EmptyCloudCredentials,
)
from ember_code.core.auth.portal_client import (
    DEFAULT_API_URL,
    DEFAULT_PORTAL_URL,
    PortalClient,
)
from ember_code.core.auth.schemas import (
    JwtClaims,
    LoadCredentialsResult,
    LoginResult,
    UserInfo,
    ValidateResult,
)

__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_PORTAL_URL",
    "CallbackServer",
    "CloudCredentials",
    "Credentials",
    "CredentialsStore",
    "EmptyCloudCredentials",
    "JwtClaims",
    "LoadCredentialsResult",
    "LoginResult",
    "PortalClient",
    "UserInfo",
    "ValidateResult",
]
