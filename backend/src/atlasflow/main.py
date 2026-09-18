from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from atlasflow.api.routes import router
from atlasflow.bootstrap import Container, build_container
from atlasflow.config import Settings, get_settings
from atlasflow.observability import configure_langsmith


def create_app(
    *, settings: Settings | None = None, container: Container | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_langsmith(settings)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Observable multi-agent research and decision platform",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.container = container or build_container(settings)
    app.include_router(router)
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
