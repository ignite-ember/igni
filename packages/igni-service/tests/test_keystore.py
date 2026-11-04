"""The service's key: sealed at rest, one per deployment, agreed on by replicas."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from igni_service import KeyCipher, KeyStoreError, MemoryKeyStore, SqlKeyStore, StoredKey
from igni_service.crypto import RsaKeyPair
from igni_service.signing import SigningKey


def a_key() -> StoredKey:
    pair = RsaKeyPair.generate()
    return StoredKey(kid=pair.thumbprint(), private_pem=pair.private_pem())


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'service.db'}")
    yield engine
    await engine.dispose()


@pytest.fixture
def cipher() -> KeyCipher:
    return KeyCipher(KeyCipher.generate())


async def test_the_sql_store_makes_its_table_and_keeps_the_key_sealed(
    engine: AsyncEngine, cipher: KeyCipher
) -> None:
    store = SqlKeyStore(engine, cipher)
    key = a_key()

    assert await store.get() is None
    assert await store.claim(key) == key
    assert await store.get() == key

    async with engine.connect() as connection:
        sealed = (
            await connection.execute(select(SqlKeyStore.table.c.private_key_encrypted))
        ).scalar_one()
    assert "PRIVATE KEY" not in sealed
    assert cipher.open(sealed) == key.private_pem


async def test_replicas_starting_together_end_up_with_one_key(
    engine: AsyncEngine, cipher: KeyCipher
) -> None:
    replicas = [SigningKey(SqlKeyStore(engine, cipher)) for _ in range(4)]

    pairs = await asyncio.gather(*(replica.pair() for replica in replicas))

    assert len({pair.thumbprint() for pair in pairs}) == 1


async def test_the_first_claim_wins_and_later_ones_get_the_winner(
    engine: AsyncEngine, cipher: KeyCipher
) -> None:
    first, second = a_key(), a_key()

    assert await SqlKeyStore(engine, cipher).claim(first) == first
    assert await SqlKeyStore(engine, cipher).claim(second) == first


async def test_a_service_that_owns_the_table_in_migrations_can_say_so(
    engine: AsyncEngine, cipher: KeyCipher
) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(SqlKeyStore.metadata.create_all)
    store = SqlKeyStore(engine, cipher, create_table=False)

    key = a_key()
    assert await store.claim(key) == key


async def test_a_changed_encryption_key_says_what_happened(
    engine: AsyncEngine, cipher: KeyCipher
) -> None:
    await SqlKeyStore(engine, cipher).claim(a_key())

    with pytest.raises(KeyStoreError, match="changed"):
        await SqlKeyStore(engine, KeyCipher(KeyCipher.generate())).get()


async def test_no_encryption_key_is_refused_before_anything_is_written(engine: AsyncEngine) -> None:
    with pytest.raises(KeyStoreError, match="IGNI_KEY_ENCRYPTION_KEY"):
        await SqlKeyStore(engine, KeyCipher("")).claim(a_key())
    with pytest.raises(KeyStoreError, match="not a Fernet key"):
        await SqlKeyStore(engine, KeyCipher("short")).claim(a_key())


async def test_the_memory_store_keeps_the_first_key() -> None:
    store = MemoryKeyStore()
    first = a_key()

    assert await store.claim(first) == first
    assert await store.claim(a_key()) == first
