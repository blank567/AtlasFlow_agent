from __future__ import annotations

import asyncio

from atlasflow.bootstrap import build_container
from atlasflow.config import Settings


async def serve() -> None:
    settings = Settings(knowledge_worker_enabled=True)
    container = build_container(settings)
    await container.knowledge.start()
    try:
        await asyncio.Event().wait()
    finally:
        await container.knowledge.close()
        await container.store.close()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
