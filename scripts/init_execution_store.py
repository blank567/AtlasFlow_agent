"""Explicit initializer; application startup only verifies these existing tables."""
from __future__ import annotations

import argparse
import asyncio

from atlasflow.config import Settings
from atlasflow.execution.postgres import (
    DEFAULT_SCHEMA,
    PostgresExecutionRepository,
    create_postgres_execution,
)
from sqlalchemy.schema import CreateSchema


async def initialize_execution_store(database_url: str, *, schema: str = DEFAULT_SCHEMA) -> None:
    repository = PostgresExecutionRepository(database_url, schema=schema)
    runtime = create_postgres_execution(database_url, schema=schema)
    try:
        async with repository.engine.begin() as connection:
            await connection.execute(CreateSchema(schema, if_not_exists=True))
            await connection.run_sync(repository.metadata.create_all)
        # setup owns its versioned checkpoint migrations. It is intentionally
        # not called by the FastAPI lifecycle.
        await runtime.checkpointer.open()
        await runtime.checkpointer.setup()
    finally:
        await runtime.close()
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    args = parser.parse_args()
    settings = Settings()
    url = getattr(settings, "execution_database_url", "") or settings.knowledge_database_url
    asyncio.run(initialize_execution_store(url, schema=args.schema))
    print("Execution storage initialized; no Run or knowledge data was modified.")


if __name__ == "__main__":
    main()
