from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1/evaluations/legacy", tags=["evaluations-legacy"])


def _service(request):
    value = getattr(request.app.state.container, "evaluation", None)
    if value is None:
        raise HTTPException(status_code=503, detail="legacy evaluation store unavailable")
    return value


@router.get("/experiments")
async def list_experiments(request: Request):
    return [
        {**row.model_dump(mode="json"), "protocol": "legacy-v1", "read_only": True}
        for row in await _service(request).store.list()
    ]


@router.get("/experiments/{experiment_id}")
async def experiment(experiment_id: str, request: Request):
    try:
        value = await _service(request).store.get(experiment_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {**value.model_dump(mode="json"), "protocol": "legacy-v1", "read_only": True}
