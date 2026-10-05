from __future__ import annotations

import mimetypes
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

from atlasflow.knowledge.api.schemas import (
    CreateSpaceRequest,
    IngestTextRequest,
    KnowledgeSearchRequest,
)
from atlasflow.knowledge.domain import PrincipalContext

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


def platform(request: Request):
    return request.app.state.container.knowledge


@router.post("/spaces", status_code=status.HTTP_201_CREATED)
async def create_space(payload: CreateSpaceRequest, request: Request):
    try:
        return await platform(request).create_space(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/spaces")
async def list_spaces(request: Request):
    return await platform(request).list_spaces()


@router.get("/spaces/{space}/documents")
async def list_documents(space: str, request: Request):
    try:
        return await platform(request).list_documents(space)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/spaces/{space}/documents", status_code=status.HTTP_202_ACCEPTED)
async def ingest_text(space: str, payload: IngestTextRequest, request: Request):
    try:
        return await platform(request).submit(
            space=space,
            title=payload.title,
            content=payload.content.encode("utf-8"),
            mime_type=payload.mime_type,
            source_id=payload.source_id,
            canonical_uri=payload.canonical_uri,
            metadata=payload.metadata,
            rights=payload.rights,
            provenance=payload.provenance,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/spaces/{space}/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    space: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
    source_id: Annotated[str | None, Form()] = None,
    canonical_uri: Annotated[str | None, Form()] = None,
):
    content = await file.read(25 * 1024 * 1024 + 1)
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="knowledge file exceeds 25 MB")
    mime_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    if mime_type not in platform(request).parsers.supported_types:
        raise HTTPException(status_code=415, detail=f"unsupported document type: {mime_type}")
    try:
        return await platform(request).submit(
            space=space,
            title=title or file.filename or "未命名文档",
            content=content,
            mime_type=mime_type,
            source_id=source_id,
            canonical_uri=canonical_uri,
            provenance={"retrieval_method": "upload", "filename": file.filename},
            rights={
                "rights_review_status": "user_asserted",
                "redistribution_policy": "private_index_only",
            },
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/documents/{document_id}")
async def get_document(document_id: str, request: Request):
    try:
        document, versions = await platform(request).get_document(document_id)
        return {"document": document, "versions": versions}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/documents/{document_id}/archive")
async def archive_document(document_id: str, request: Request):
    try:
        return await platform(request).archive_document(document_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs")
async def list_jobs(request: Request, space: str | None = None):
    try:
        return await platform(request).list_jobs(space)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    try:
        return await platform(request).get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_job(job_id: str, request: Request):
    try:
        return await platform(request).retry_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/search")
@router.post("/search/debug")
async def search(payload: KnowledgeSearchRequest, request: Request):
    try:
        return await platform(request).search(
            payload.query,
            space=payload.space,
            filters=payload.filters,
            result_limit=payload.result_limit,
            principal=PrincipalContext(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
