from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from atlasflow.config import get_settings
from atlasflow.knowledge.infrastructure.postgres_repository import metadata
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
settings = get_settings()
database_url = (
    settings.knowledge_database_url
    if "knowledge_database_url" in settings.model_fields_set
    else config.get_main_option("sqlalchemy.url")
)
# Resolve database settings consistently with the API, including .env values.
# ConfigParser treats percent signs specially, including encoded passwords.
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
