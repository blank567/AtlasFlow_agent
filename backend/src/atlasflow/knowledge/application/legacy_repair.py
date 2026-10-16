"""Opt-in state repair. Preview never changes jobs or calls a provider."""

from atlasflow.knowledge.domain import JobStatus
from atlasflow.knowledge.ports.repository import KnowledgeRepository


async def repair_legacy_jobs(repository: KnowledgeRepository, space: str,
                             *, confirm_no_workers: bool = False) -> dict:
    selected = await repository.get_space(space)
    if selected is None:
        raise KeyError(f"knowledge space not found: {space}")
    candidates = [job for job in await repository.list_jobs(selected.id)
                  if job.status == JobStatus.RUNNING
                  and not (job.generation_id and job.build_request_hash)]
    results = []
    for job in candidates:
        if confirm_no_workers:
            job = await repository.repair_legacy_running_job(job.id)
        results.append({"job_id": job.id, "document_id": job.document_id,
                        "space_build_id": job.space_build_id, "status": job.status.value,
                        "error_code": job.error_code})
    return {"dry_run": not confirm_no_workers, "space_id": selected.id, "jobs": results,
            "sources_deleted": False}
