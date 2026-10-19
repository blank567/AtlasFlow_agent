from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_id: str
    candidate_id: str
    scope: Literal["configuration", "retrieval", "index", "agent"] = "configuration"


class RegisterArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: dict[str, Any]
    case_id: str
    variant_id: str
    trial: int = Field(default=1, ge=1, le=5)
    artifact: dict[str, Any]


class CompareVariantsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_variant_id: str | None = Field(default=None, min_length=1, max_length=64)
    candidate_variant_id: str | None = Field(default=None, min_length=1, max_length=64)


class DeletedExperimentResponse(BaseModel):
    experiment_id: str
    kind: Literal["rag", "agent"]
    status: Literal["deleted"]
    deleted_at: str
    artifacts_retained: Literal[True] = Field(description="Evidence and original data are not purged")


def create_v2_router(kind: Literal["rag", "agent"], runtime=None) -> APIRouter:
    if kind not in {"rag", "agent"}:
        raise ValueError("unknown evaluation workspace")
    router = APIRouter(prefix=f"/api/v1/evaluations/{kind}", tags=[f"evaluations-{kind}"])

    def service(request):
        value = runtime or getattr(request.app.state.container, "evaluation_v2", None)
        if value is None:
            raise HTTPException(status_code=503, detail="evaluation v2 service is not configured")
        return value

    async def translated(callback, *, conflict=False):
        try:
            return await callback()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409 if conflict else 422, detail=str(exc)) from exc

    @router.get("/datasets")
    async def datasets(request: Request):
        return service(request).list_datasets(kind)

    @router.get("/settings")
    async def settings(request: Request):
        return service(request).settings(kind)

    if kind == "agent":
        @router.get("/inputs")
        async def inputs(request: Request):
            return await service(request).list_inputs(kind)

        @router.post("/inputs", status_code=201)
        async def register_input(payload: RegisterArtifactRequest, request: Request):
            value = service(request)
            async def register():
                value._plan(payload.plan, kind)
                return await value.register_input(**payload.model_dump())
            result = await translated(register)
            return value.input_metadata(result)

    @router.post("/preflight")
    async def preflight(payload: dict[str, Any], request: Request):
        return await translated(lambda: service(request).preflight(payload, kind))

    @router.post("/experiments", status_code=202)
    async def create(payload: dict[str, Any], request: Request):
        return await translated(lambda: service(request).create(payload, kind), conflict=True)

    @router.get("/experiments")
    async def experiments(request: Request):
        return await service(request).store.list_summaries(kind)

    @router.get("/experiments/{experiment_id}")
    async def experiment(experiment_id: str, request: Request):
        return await translated(lambda: service(request).store.get(experiment_id, kind))

    @router.delete("/experiments/{experiment_id}", response_model=DeletedExperimentResponse,
                   summary="Soft-delete a terminal experiment; retain evidence",
                   responses={404: {"description": "Missing or outside this workspace"},
                              409: {"description": "Still executing or finalizing; cancel or wait first"}})
    async def delete_experiment(experiment_id: str, request: Request):
        return await translated(lambda: service(request).delete(experiment_id, kind), conflict=True)

    @router.get("/experiments/{experiment_id}/artifacts/{input_id}")
    async def artifact(experiment_id: str, input_id: str, request: Request):
        return await translated(lambda: service(request).artifact(experiment_id, input_id, kind))

    @router.get("/experiments/{experiment_id}/comparison")
    async def variant_comparison(experiment_id: str, request: Request, baseline_variant_id: str | None = None, candidate_variant_id: str | None = None):
        return await translated(lambda: service(request).compare_variants(experiment_id, baseline_variant_id, candidate_variant_id, kind))

    @router.post("/experiments/{experiment_id}/compare")
    async def compare_variants(experiment_id: str, payload: CompareVariantsRequest, request: Request):
        return await translated(lambda: service(request).compare_variants(experiment_id, payload.baseline_variant_id, payload.candidate_variant_id, kind))

    @router.get("/experiments/{experiment_id}/export")
    async def export(experiment_id: str, request: Request, format: Literal["json", "csv"] = "json"):
        content = await translated(lambda: service(request).export(experiment_id, format, kind))
        return Response(
            content, media_type="application/json" if format == "json" else "text/csv",
            headers={"Content-Disposition": f'attachment; filename="{kind}-evaluation-v2.{format}"'},
        )

    @router.post("/experiments/{experiment_id}/cancel")
    async def cancel(experiment_id: str, request: Request):
        return await translated(lambda: service(request).cancel(experiment_id, kind), conflict=True)

    @router.post("/compare")
    async def compare(payload: CompareRequest, request: Request):
        return await translated(lambda: service(request).compare(payload.baseline_id, payload.candidate_id, payload.scope, kind), conflict=True)

    return router
