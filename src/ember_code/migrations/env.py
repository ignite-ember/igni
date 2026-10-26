"""Alembic environment for ember-code.

Imports every ORM model module so ``Base.metadata`` is fully populated
before ``--autogenerate`` diffs against the live schema.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ember_code.core.approvals import journal as _approval_journal  # noqa: F401
from ember_code.core.db.base import Base
from ember_code.core.loop import db_models as _loop_models  # noqa: F401
from ember_code.core.scheduler import db_models as _scheduler_models  # noqa: F401
from ember_code.core.session import client_state as _client_state  # noqa: F401
from ember_code.core.session import pending_messages as _pending_messages  # noqa: F401
from ember_code.core.session import queued_messages as _queued_messages  # noqa: F401
from ember_code.core.session import session_directories as _session_directories  # noqa: F401
from ember_code.core.session import session_preferences as _session_preferences  # noqa: F401
from ember_code.core.tools import process_store as _process_store  # noqa: F401
from ember_code.core.utils import attachment_store as _attachment_store  # noqa: F401
from ember_code.core.utils import memory_files as _memory_files  # noqa: F401
from ember_code.extensions import settings_values as _extension_settings  # noqa: F401
from ember_code.notifications import local_store as _local_notifications  # noqa: F401

config = context.config

# ``fileConfig`` reconfigures the *root* logger from alembic.ini, which
# replaces every handler the application installed — including the
# ``--debug`` file handler — with a NOTSET stderr handler. Correct when
# someone runs the ``alembic`` CLI, wrong when we upgrade in-process on
# every startup, so the programmatic caller opts out via this attribute.
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
