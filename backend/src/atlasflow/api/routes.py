from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from math import ceil
from time import monotonic
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from atlasflow import __version__
from atlasflow.bootstrap import Container
from atlasflow.observability import (
    ObservabilityMeta,
    check_langsmith_connection,
    get_observability_meta,
)
from atlasflow.schemas import (
    AnalyticsResponse,
    ApprovalRequest,
    AppSettingsStatus,
    CreateRunRequest,
    DatabaseSettingsStatus,
    DeleteRunRequest,
    IngestDocumentRequest,
    IngestDocumentResponse,
    LangSmithSettingsStatus,
    ProviderSettingsStatus,
    RerunRequest,
    RunListItem,
    RunListResponse,
    RunRecord,
    RunStatus,
)
from atlasflow.service import InvalidRunStateError, RunNotFoundError

router = APIRouter(prefix="/api/v1")


def container_from(request: Request) -> Container:
    return request.app.state.container


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    container = container_from(request)
    knowledge = await container.knowledge.stats()
    return {
        "status": "ok",
        "knowledge": knowledge,
        "knowledge_chunks": knowledge["chunks"],
        "tools": len(container.registry.describe()),
    }


@router.get("/tools")
async def list_tools(request: Request) -> list[dict[str, object]]:
    return container_from(request).registry.describe()


@router.get("/capabilities")
async def list_capabilities(request: Request) -> list[dict[str, object]]:
    return container_from(request).registry.capability_catalog()


@router.post("/documents", response_model=IngestDocumentResponse)
async def ingest_document(
    payload: IngestDocumentRequest, request: Request
) -> IngestDocumentResponse:
    knowledge = container_from(request).knowledge
    job = await knowledge.submit(
        space=knowledge.default_space,
        title=payload.title,
        content=payload.content.encode("utf-8"),
        mime_type="text/plain",
        source_id=payload.source_id,
        metadata=payload.metadata,
    )
    job = await knowledge.process_job(job.id)
    count = await knowledge.repository.count_version_chunks(job.version_id)
    return IngestDocumentResponse(document_id=job.document_id, chunks_created=count)


@router.post("/runs", response_model=RunRecord, status_code=status.HTTP_202_ACCEPTED)
async def create_run(payload: CreateRunRequest, request: Request) -> RunRecord:
    return await container_from(request).run_service.create(
        payload.query,
        auto_approve=payload.auto_approve,
        policy=payload.policy,
    )


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    request: Request,
    query: str | None = Query(default=None, min_length=1, max_length=4000),
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> RunListResponse:
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None and value.utcoffset() is None:
            raise HTTPException(
                status_code=422,
                detail=f"{name} must include a timezone offset",
            )
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not exceed date_to")
    items, total = await container_from(request).store.list(
        query=query,
        status=run_status,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    return RunListResponse(
        items=[RunListItem.from_record(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        pages=ceil(total / page_size) if total else 0,
    )


@router.get("/analytics", response_model=AnalyticsResponse)
async def analytics(
    request: Request,
    selected_range: Literal["7d", "30d", "all"] = Query(default="30d", alias="range"),
) -> AnalyticsResponse:
    return await container_from(request).run_service.analytics(selected_range)


@router.get("/settings/status", response_model=AppSettingsStatus)
async def settings_status(request: Request) -> AppSettingsStatus:
    container = container_from(request)
    settings = container.settings

    return AppSettingsStatus(
        app_version=__version__,
        database=DatabaseSettingsStatus(
            path=container.store.database_path,
            size_bytes=container.store.size_bytes,
            persistent=container.store.database_path != ":memory:",
        ),
        langsmith=_langsmith_settings_status(get_observability_meta()),
        provider=ProviderSettingsStatus(
            configured=bool(settings.llm_api_key and settings.llm_model),
            name=settings.llm_provider,
            model=settings.llm_model or None,
        ),
    )


@router.post(
    "/settings/langsmith/check",
    response_model=LangSmithSettingsStatus,
)
async def check_langsmith() -> LangSmithSettingsStatus:
    return _langsmith_settings_status(await check_langsmith_connection())


@router.get("/runs/{run_id}", response_model=RunRecord)
async def get_run(run_id: str, request: Request) -> RunRecord:
    try:
        return await container_from(request).store.get(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


@router.post(
    "/runs/{run_id}/rerun",
    response_model=RunRecord,
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/runs/{run_id}/clone",
    response_model=RunRecord,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
async def rerun(
    run_id: str,
    request: Request,
    payload: RerunRequest | None = None,
) -> RunRecord:
    payload = payload or RerunRequest()
    try:
        return await container_from(request).run_service.rerun(
            run_id,
            query=payload.query,
            auto_approve=payload.auto_approve,
            policy=payload.policy,
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunRecord,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_run(run_id: str, request: Request) -> RunRecord:
    try:
        return await container_from(request).run_service.cancel(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except InvalidRunStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: str,
    request: Request,
    payload: DeleteRunRequest | None = None,
    confirmation_run_id: str | None = Query(default=None),
) -> Response:
    confirmation = payload.confirmation_run_id if payload is not None else confirmation_run_id
    if confirmation is None:
        raise HTTPException(status_code=422, detail="confirmation_run_id is required")
    try:
        await container_from(request).run_service.delete(run_id, confirmation)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except InvalidRunStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/runs/{run_id}/approval",
    response_model=RunRecord,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resolve_run_approval(
    run_id: str, payload: ApprovalRequest, request: Request
) -> RunRecord:
    try:
        return await container_from(request).run_service.resolve_approval(run_id, payload)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except InvalidRunStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    last_event_id: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
    container = container_from(request)
    try:
        await container.store.get(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc

    header_cursor = request.headers.get("last-event-id")
    if header_cursor is not None:
        try:
            cursor = int(header_cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Last-Event-ID must be a non-negative integer"
            ) from exc
        if cursor < 0:
            raise HTTPException(
                status_code=400, detail="Last-Event-ID must be a non-negative integer"
            )
    else:
        cursor = last_event_id or 0

    async def event_stream() -> AsyncIterator[str]:
        nonlocal cursor
        last_heartbeat = monotonic()
        yield "retry: 1500\n\n"
        while True:
            if await request.is_disconnected():
                break
            events, run_status = await container.store.event_batch(run_id, cursor)
            for event in events:
                cursor = event.sequence
                yield (
                    f"event: {event.event_type.value}\n"
                    f"id: {event.sequence}\n"
                    f"data: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                )
            if run_status in RunStatus.terminal():
                yield (
                    "event: done\n"
                    f"id: {cursor}\n"
                    f"data: {json.dumps({'status': run_status.value})}\n\n"
                )
                break
            if monotonic() - last_heartbeat >= container.settings.sse_heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_heartbeat = monotonic()
            await asyncio.sleep(container.settings.sse_poll_seconds)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _langsmith_settings_status(meta: ObservabilityMeta) -> LangSmithSettingsStatus:
    if meta.connection_status == "unreachable" or meta.status == "degraded":
        state = "unreachable"
    elif meta.status == "disabled":
        state = "disabled"
    elif meta.status == "not_configured":
        state = "not_configured"
    else:
        state = "ready"
    return LangSmithSettingsStatus(
        state=state,
        connection_status=meta.connection_status,
        configured=meta.configured,
        enabled=meta.enabled,
        project=meta.project,
        endpoint=meta.endpoint,
        trace_content="full" if meta.trace_content else "metadata",
        last_checked_at=meta.last_checked_at,
        message=meta.message,
    )
