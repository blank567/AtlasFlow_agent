from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from atlasflow.bootstrap import Container
from atlasflow.schemas import (
    CreateRunRequest,
    IngestDocumentRequest,
    IngestDocumentResponse,
    RunRecord,
    RunStatus,
)
from atlasflow.service import RunNotFoundError

router = APIRouter(prefix="/api/v1")


def container_from(request: Request) -> Container:
    return request.app.state.container


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    container = container_from(request)
    return {
        "status": "ok",
        "knowledge_chunks": container.retriever.chunk_count,
        "tools": len(container.registry.describe()),
    }


@router.get("/tools")
async def list_tools(request: Request) -> list[dict[str, object]]:
    return container_from(request).registry.describe()


@router.post("/documents", response_model=IngestDocumentResponse)
async def ingest_document(
    payload: IngestDocumentRequest, request: Request
) -> IngestDocumentResponse:
    document_id, count = container_from(request).retriever.ingest(
        title=payload.title,
        content=payload.content,
        source_id=payload.source_id,
        metadata=payload.metadata,
    )
    return IngestDocumentResponse(document_id=document_id, chunks_created=count)


@router.post("/runs", response_model=RunRecord, status_code=status.HTTP_202_ACCEPTED)
async def create_run(payload: CreateRunRequest, request: Request) -> RunRecord:
    return await container_from(request).run_service.create(payload.query)


@router.get("/runs/{run_id}", response_model=RunRecord)
async def get_run(run_id: str, request: Request) -> RunRecord:
    try:
        return await container_from(request).store.get(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


@router.get("/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    container = container_from(request)
    try:
        await container.store.get(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc

    async def event_stream() -> AsyncIterator[str]:
        cursor = 0
        while True:
            if await request.is_disconnected():
                break
            run = await container.store.get(run_id)
            while cursor < len(run.events):
                event = run.events[cursor]
                cursor += 1
                yield f"event: {event.type}\ndata: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                yield f"event: done\ndata: {json.dumps({'status': run.status.value})}\n\n"
                break
            await asyncio.sleep(0.25)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

