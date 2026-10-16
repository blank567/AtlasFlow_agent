from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from atlasflow.evaluation.contracts import EvaluationArm, EvaluationPlan

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])


def service(request):
    value = getattr(request.app.state.container, "evaluation", None)
    if value is None:
        raise HTTPException(status_code=503, detail="evaluation service is not configured")
    return value


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_id: str
    candidate_id: str
    scope: Literal["retrieval", "index"] = "retrieval"


class RegisterInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str
    case_id: str
    arm: EvaluationArm
    run_id: str | None = None
    content: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    source_id_map: dict[str, str] | None = None
    source_hash_map: dict[str, str] | None = None


@router.get("/datasets")
async def datasets(request: Request):
    return service(request).datasets.list()


@router.get("/settings")
async def settings(request: Request):
    return service(request).settings()


@router.get("/inputs")
async def inputs(request: Request):
    runtime = service(request)
    return [runtime.input_metadata(row) for row in await runtime.store.list_inputs()]


@router.post("/inputs", status_code=201)
async def register_input(payload: RegisterInputRequest, request: Request):
    try:
        value = await service(request).register_input(
            **payload.model_dump(exclude={"arm"}), arm=payload.arm
        )
        return service(request).input_metadata(value)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/preflight")
async def preflight(payload: EvaluationPlan, request: Request):
    try:
        return await service(request).preflight(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("", status_code=202)
async def create(payload: EvaluationPlan, request: Request):
    try:
        return await service(request).create(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("")
async def experiments(request: Request):
    return await service(request).store.list()


@router.post("/compare")
async def compare(payload: CompareRequest, request: Request):
    try:
        return await service(request).compare(
            payload.baseline_id, payload.candidate_id, payload.scope
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{experiment_id}")
async def experiment(experiment_id: str, request: Request):
    try:
        return await service(request).store.get(experiment_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{experiment_id}/cases")
async def cases(experiment_id: str, request: Request):
    return (await experiment(experiment_id, request)).results


@router.post("/{experiment_id}/cancel")
async def cancel(experiment_id: str, request: Request):
    try:
        return await service(request).cancel(experiment_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{experiment_id}/export")
async def export(experiment_id: str, request: Request, format: Literal["json", "csv"] = "json"):
    try:
        content = await service(request).export(experiment_id, format)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    filename = "evaluation.json" if format == "json" else "evaluation.csv"
    return Response(
        content,
        media_type="application/json" if format == "json" else "text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
