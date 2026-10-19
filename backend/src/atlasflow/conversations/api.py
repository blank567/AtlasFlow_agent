"""Versioned conversation snapshots over SSE; no browser-only memory."""

from __future__ import annotations

import asyncio
import json
from time import monotonic

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from atlasflow.conversations.contracts import (
    ConfirmTurn,
    ConversationConflict,
    ConversationNotFoundError,
    CreateConversation,
    DeleteConversation,
    SendMessage,
)
from atlasflow.execution.contracts import ExecutionControlError, PersistenceUnavailable
from atlasflow.service import InvalidRunStateError, RunNotFoundError

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


def service(request: Request):
    value = request.app.state.container.conversations
    if value is None:
        raise HTTPException(status_code=503, detail="会话服务未启用")
    return value


def translate(exc):
    if isinstance(exc, (ConversationNotFoundError, RunNotFoundError)):
        return HTTPException(status_code=404, detail="会话、轮次或来源报告不存在")
    if isinstance(exc, PersistenceUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, (ConversationConflict, InvalidRunStateError, ExecutionControlError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.post("", status_code=201)
async def create(payload: CreateConversation, request: Request):
    if payload.policy.knowledge_mode == "selected":
        try:
            await request.app.state.container.knowledge.get_space(payload.policy.knowledge_space)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="所选知识空间不存在") from exc
    try:
        return await service(request).create(payload)
    except ValueError as exc:
        raise translate(exc) from exc


@router.get("")
async def list_conversations(
    request: Request, page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100)
):
    values, total = await service(request).store.list(page=page, page_size=page_size)
    return {"items": values, "total": total, "page": page, "page_size": page_size}


@router.get("/{conversation_id}")
async def detail(conversation_id: str, request: Request):
    try:
        return await service(request).detail(conversation_id)
    except (KeyError, ValueError) as exc:
        raise translate(exc) from exc


@router.delete("/{conversation_id}")
async def delete(conversation_id: str, payload: DeleteConversation, request: Request):
    try:
        return await service(request).delete(conversation_id, payload)
    except (KeyError, ValueError, InvalidRunStateError, ExecutionControlError) as exc:
        raise translate(exc) from exc


@router.post("/{conversation_id}/messages", status_code=202)
async def send(conversation_id: str, payload: SendMessage, request: Request):
    try:
        return await service(request).send(conversation_id, payload)
    except (KeyError, ValueError) as exc:
        raise translate(exc) from exc


@router.post("/{conversation_id}/turns/{turn_id}/confirm", status_code=202)
async def confirm(conversation_id: str, turn_id: str, payload: ConfirmTurn, request: Request):
    try:
        return await service(request).confirm(conversation_id, turn_id, payload)
    except (KeyError, ValueError, InvalidRunStateError, ExecutionControlError) as exc:
        raise translate(exc) from exc


@router.post("/{conversation_id}/turns/{turn_id}/cancel")
async def cancel(conversation_id: str, turn_id: str, payload: ConfirmTurn, request: Request):
    if payload.confirmation_turn_id != turn_id:
        raise HTTPException(status_code=409, detail="停止确认与当前轮次不一致")
    try:
        return await service(request).cancel(conversation_id, turn_id)
    except (KeyError, ValueError, InvalidRunStateError, ExecutionControlError) as exc:
        raise translate(exc) from exc


@router.get("/{conversation_id}/events")
async def events(conversation_id: str, request: Request, last_event_id: int = Query(0, ge=0)):
    instance = service(request)
    try:
        await instance.detail(conversation_id)
    except (KeyError, ValueError) as exc:
        raise translate(exc) from exc
    cursor_text = request.headers.get("last-event-id")
    try:
        cursor = int(cursor_text) if cursor_text is not None else last_event_id
        if cursor < 0:
            raise ValueError("negative cursor")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID必须为非负整数") from exc

    async def stream():
        nonlocal cursor
        first = True
        heartbeat = monotonic()
        yield "retry: 1500\n\n"
        while not await request.is_disconnected():
            try:
                snapshot = await instance.detail(conversation_id)
            except ConversationNotFoundError:
                yield f"event: conversation_deleted\ndata: {json.dumps({'conversation_id': conversation_id})}\n\n"
                return
            version = snapshot.conversation.version
            if first or version > cursor:
                yield f"event: conversation_snapshot\nid: {version}\ndata: {json.dumps(snapshot.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                cursor, first = version, False
            if monotonic() - heartbeat > 15:
                yield "event: heartbeat\ndata: {}\n\n"
                heartbeat = monotonic()
            await asyncio.sleep(max(0.5, request.app.state.container.settings.sse_poll_seconds))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
