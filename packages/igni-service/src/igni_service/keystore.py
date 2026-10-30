"""Where a service keeps its own private key.

The key is the deployment's identity: igni registered its public half
and a super admin approved the service by its thumbprint. So every
replica must sign with the same key, and replicas starting together must
end up agreeing on one. That is the whole contract of a `KeyStore`:

* `get()` — the kept key, or `None` before there is one;
* `claim(key)` — keep `key` unless one is already kept, and answer with
  whichever is. Two replicas that each generated a key both call it; one
  wins, and both go on to sign with the winner's.

The private half is sealed with `KeyCipher` before it is stored, so a
store holds ciphertext only. `MemoryKeyStore` is for tests;
`igni_service.sql_keystore.SqlKeyStore` is for a deployment.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict

from igni_service.errors import KeyStoreError


class StoredKey(BaseModel):
    """A kept key: its `kid` (thumbprint) and its private half, unsealed."""

    model_config = ConfigDict(frozen=True)

    kid: str
    private_pem: str


@runtime_checkable
class KeyStore(Protocol):
    """Keeps one key. See the module note for the contract."""

    async def get(self) -> StoredKey | None: ...

    async def claim(self, key: StoredKey) -> StoredKey: ...


class KeyCipher:
    """Fernet under the service's key encryption key."""

    def __init__(self, key: str) -> None:
        self._key = (key or "").strip()

    @staticmethod
    def generate() -> str:
        """A new key encryption key, for an operator to keep."""
        return Fernet.generate_key().decode()

    def _fernet(self) -> Fernet:
        if not self._key:
            raise KeyStoreError(
                "IGNI_KEY_ENCRYPTION_KEY is not set, so this service cannot keep its own key."
            )
        try:
            return Fernet(self._key.encode())
        except (ValueError, TypeError) as exc:
            raise KeyStoreError("IGNI_KEY_ENCRYPTION_KEY is not a Fernet key.") from exc

    def seal(self, plaintext: str) -> str:
        return self._fernet().encrypt(plaintext.encode()).decode()

    def open(self, sealed: str) -> str:
        try:
            return self._fernet().decrypt(sealed.encode()).decode()
        except InvalidToken as exc:
            raise KeyStoreError(
                "This service's key cannot be read with this IGNI_KEY_ENCRYPTION_KEY: it was "
                "changed. Restore the old one, or delete the stored key to make a new identity "
                "(which a super admin must approve again)."
            ) from exc


class MemoryKeyStore:
    """A key kept in this process only. For tests; a restart is a new identity."""

    def __init__(self) -> None:
        self._kept: StoredKey | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> StoredKey | None:
        return self._kept

    async def claim(self, key: StoredKey) -> StoredKey:
        async with self._lock:
            if self._kept is None:
                self._kept = key
            return self._kept
