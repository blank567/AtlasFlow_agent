from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from atlasflow import __version__
from atlasflow.api.routes import router
from atlasflow.bootstrap import Container, build_container
from atlasflow.config import Settings, get_settings
from atlasflow.conversations.api import router as conversation_router
from atlasflow.evaluation.api import router as evaluation_router
from atlasflow.evaluation.common.api import create_v2_router
from atlasflow.evaluation.legacy.api import router as legacy_evaluation_router
from atlasflow.knowledge.api import router as knowledge_router
from atlasflow.observability import configure_langsmith


def create_app(*, settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_langsmith(settings)
    resolved_container = container or build_container(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            if resolved_container.execution is not None:
                await resolved_container.execution.start()
                await resolved_container.knowledge.repository.initialize()
            await resolved_container.store.open()
            if resolved_container.conversations is not None:
                await resolved_container.conversations.store.initialize()
            await resolved_container.knowledge.start()
            await resolved_container.run_service.reconcile_startup()
            if resolved_container.conversations is not None:
                await resolved_container.conversations.reconcile_startup()
            if resolved_container.evaluation is not None:
                await resolved_container.evaluation.initialize(recover_interrupted=False)
            if resolved_container.evaluation_v2 is not None:
                await resolved_container.evaluation_v2.initialize(recover_interrupted=True)
            yield
        finally:
            if resolved_container.conversations is not None:
                await resolved_container.conversations.close()
            if resolved_container.evaluation_v2 is not None:
                await resolved_container.evaluation_v2.close()
            if resolved_container.evaluation is not None:
                await resolved_container.evaluation.close()
            await resolved_container.run_service.close()
            await resolved_container.knowledge.close()
            close = getattr(resolved_container.store, "close", None)
            if close is not None:
                await close()
            if resolved_container.execution is not None:
                await resolved_container.execution.close()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="Observable multi-agent research and decision platform",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.container = resolved_container
    app.include_router(router)
    app.include_router(conversation_router)
    app.include_router(knowledge_router)
    app.include_router(create_v2_router("rag"))
    app.include_router(create_v2_router("agent"))
    app.include_router(legacy_evaluation_router)
    app.include_router(evaluation_router)
    return app


def run() -> None:
    import uvicorn

    uvicorn.run(
        "atlasflow.main:create_app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        factory=True,
    )
