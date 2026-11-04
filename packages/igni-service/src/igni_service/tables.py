"""The tables this SDK keeps in a service's own database.

All on one `MetaData` of their own, so they never mix into the service's
models, and a service that owns its schema in migrations includes this
one in Alembic's `target_metadata` (a list of metadatas is accepted):

    target_metadata = [Base.metadata, IgniTables.metadata]

and passes `create_table=False` to the stores. Otherwise each store
creates its own tables on first use.

* `igni_service_key` — the service's private key, one row, sealed.
* `igni_web_session` — people signed in to the service's own pages:
  their tokens sealed, the session id stored only as a hash.
* `igni_web_login` — sign-ins in flight: the PKCE verifier, sealed, for
  ten minutes.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Float, MetaData, Table, Text


class IgniTables:
    metadata = MetaData()

    service_key = Table(
        "igni_service_key",
        metadata,
        Column("name", Text, primary_key=True),
        #: The public key's RFC 7638 thumbprint, which is also its `kid`.
        Column("kid", Text, nullable=False),
        #: PKCS#8 PEM, Fernet ciphertext under the key encryption key.
        Column("private_key_encrypted", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )

    web_session = Table(
        "igni_web_session",
        metadata,
        #: SHA-256 of the cookie's value; the value itself is never stored.
        Column("id_hash", Text, primary_key=True),
        #: The session, as JSON, Fernet ciphertext.
        Column("sealed", Text, nullable=False),
        #: When the refresh token runs out: past this the row is dead.
        Column("expires_at", Float, nullable=False, index=True),
    )

    web_login = Table(
        "igni_web_login",
        metadata,
        #: SHA-256 of the `state` sent to igni.
        Column("state_hash", Text, primary_key=True),
        Column("sealed", Text, nullable=False),
        Column("expires_at", Float, nullable=False, index=True),
    )
