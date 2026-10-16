from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from atlasflow.evaluation.contracts import EvaluationExperiment


@contextmanager
def private_evaluation_tracing():
    """Never inherit production full-content tracing into an evaluation."""
    try:
        from langsmith import tracing_context
    except ImportError:
        yield
        return
    from atlasflow.observability import local_observability_only

    with tracing_context(enabled=False), local_observability_only():
        yield


@contextmanager
def provider_capture():
    # A separate collector does not force-enable a remote workflow trace.
    from atlasflow.observability import capture_provider_calls

    with capture_provider_calls() as calls:
        yield calls


def safe_summary(experiment: EvaluationExperiment) -> dict:
    return {
        "experiment_id": experiment.experiment_id,
        "dataset_id": experiment.plan.dataset_id,
        "dataset_hash": experiment.dataset_hash,
        "comparison_key": experiment.comparison_key,
        "status": experiment.status,
        "mode": experiment.plan.mode,
        "budget": experiment.budget_ledger,
        "cases": [
            {
                "case_id": row.case_id,
                "arm_id": row.arm_id,
                "status": row.status,
                "metrics": row.metrics,
                "replayed": row.replayed,
            }
            for row in experiment.results
        ],
        "content_redacted": True,
    }


class MetadataLangSmithAdapter:
    def __init__(self, client=None, project: str = "atlasflow-evaluations"):
        self.client = client
        self.project = project

    async def sync(self, experiment: EvaluationExperiment) -> str:
        if self.client is None:
            return "not_configured"
        import asyncio

        from atlasflow.evaluation.contracts import utc_now

        payload = safe_summary(experiment)
        run_id = uuid4()
        try:
            await asyncio.to_thread(
                self.client.create_run,
                id=run_id,
                name="atlasflow.evaluation.summary",
                run_type="chain",
                inputs={"experiment_id": experiment.experiment_id, "content_redacted": True},
                project_name=self.project,
                start_time=experiment.created_at,
                extra={"metadata": {"dataset_hash": experiment.dataset_hash}},
            )
            await asyncio.to_thread(
                self.client.update_run, run_id, outputs=payload, end_time=utc_now()
            )
            await asyncio.to_thread(self.client.flush)
        except Exception:  # noqa: BLE001 - optional telemetry must not affect local results
            return "failed"
        return "synced_metadata_only"
