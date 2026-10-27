"""The cloud's credentials: what the VM keeps, never ``credentials.json``.

``docs/CLOUD.md`` §9. The token igni uses is the one it keeps from the
VM's own credential (:mod:`igni_cloud.guest.credential`), in the file
``IGNI_ACCESS_TOKEN_FILE`` names; read on every credentials load, so a
renewed token reaches every part of igni without a restart.
``IGNI_ACCESS_TOKEN`` (fixed for the process) is for a VM started without
the guest contract. Saving and clearing write nothing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ember_code.core.auth.credentials import Credentials, CredentialSource
from ember_code.core.auth.schemas import LoadCredentialsResult

logger = logging.getLogger(__name__)


class EnvironmentCredentials(CredentialSource):
    """The token in the environment, never a ``credentials.json``. The first set wins:

    * ``IGNI_ACCESS_TOKEN_FILE`` — a file holding the token, rewritten
      (temporary file, rename) whenever it is renewed — by the VM's
      credential keeper. Read on every credentials load, so a fresh token
      reaches a running VM — after a resume too — without a restart;
    * ``IGNI_ACCESS_TOKEN`` — the token itself, fixed for the process's
      life.

    ``IGNI_ACCESS_TOKEN_EXPIRES_AT`` and ``IGNI_EMAIL`` are optional.
    """

    TOKEN_FILE_ENV = "IGNI_ACCESS_TOKEN_FILE"
    TOKEN_ENV = "IGNI_ACCESS_TOKEN"
    EXPIRES_ENV = "IGNI_ACCESS_TOKEN_EXPIRES_AT"
    EMAIL_ENV = "IGNI_EMAIL"

    @classmethod
    def token(cls) -> str:
        path = os.environ.get(cls.TOKEN_FILE_ENV, "").strip()
        if path:
            try:
                return Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning("cannot read the token at %s: %s", path, exc)
                return ""
        return os.environ.get(cls.TOKEN_ENV, "").strip()

    def load(self) -> LoadCredentialsResult:
        token = self.token()
        if not token:
            return LoadCredentialsResult(ok=False, reason="no_file")
        return LoadCredentialsResult(
            ok=True,
            creds=Credentials(
                access_token=token,
                email=os.environ.get(self.EMAIL_ENV, ""),
                expires_at=os.environ.get(self.EXPIRES_ENV, ""),
            ),
        )


__all__ = ["EnvironmentCredentials"]
