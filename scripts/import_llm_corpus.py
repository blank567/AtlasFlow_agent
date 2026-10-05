"""Explicitly download and ingest selected LLM corpus sources from the manifest.

Nothing runs at application startup. Raw files and the import ledger stay under
the ignored E-drive knowledge data directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from atlasflow.bootstrap import build_container
from atlasflow.config import PROJECT_ROOT, Settings
from atlasflow.knowledge.domain import JobStatus, VersionStatus
from atlasflow.knowledge.manifest import ManifestSource, load_manifest

DEFAULT_SOURCES = (
    "llm-survey-2026",
    "attention-is-all-you-need",
    "chinchilla",
    "instructgpt",
    "lora",
    "flashattention",
    "pagedattention",
    "rag-survey",
    "react",
    "multi-agent-survey",
    "qwen2.5-report",
    "deepseek-v3-report",
    "transformers-kv-cache",
    "transformers-cache-explanation",
    "transformers-optimization",
    "transformers-continuous-batching",
    "microsoft-llmwiki-readme",
    "microsoft-llmwiki-architecture",
)
MAX_SOURCE_BYTES = 25 * 1024 * 1024
ALLOWED_DOWNLOAD_HOSTS = {
    "arxiv.org",
    "export.arxiv.org",
    "idp.springer.com",
    "link.springer.com",
    "raw.githubusercontent.com",
}


def source_url(source: ManifestSource) -> str:
    if source.source_type == "paper" and source.arxiv_id:
        return f"https://arxiv.org/pdf/{quote(source.arxiv_id)}"
    if source.source_id == "llm-survey-2026" and source.doi:
        return f"https://link.springer.com/content/pdf/{quote(source.doi, safe='/')}.pdf"
    if source.source_type == "github_document" and source.repository and source.path:
        repository = urlparse(source.repository)
        if repository.scheme != "https" or repository.netloc != "github.com":
            raise ValueError(f"unsupported repository URL for {source.source_id}")
        segments = [segment for segment in repository.path.strip("/").split("/") if segment]
        if len(segments) != 2 or any(segment in {".", ".."} for segment in segments):
            raise ValueError(f"invalid repository path for {source.source_id}")
        path = source.path.replace("\\", "/").strip("/")
        if not path or any(segment in {".", ".."} for segment in path.split("/")):
            raise ValueError(f"invalid source path for {source.source_id}")
        ref = source.resolved_commit or source.upstream_ref or "main"
        return (
            "https://raw.githubusercontent.com/"
            f"{segments[0]}/{segments[1]}/{quote(ref, safe='')}/{quote(path, safe='/')}"
        )
    raise ValueError(f"no supported full-text download URL for {source.source_id}")


def source_path(root: Path, source: ManifestSource) -> Path:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", source.source_id) is None:
        raise ValueError(f"invalid source ID for local filename: {source.source_id}")
    suffix = ".pdf" if source.source_type == "paper" else ".md"
    return root / f"{source.source_id}{suffix}"


def validate_content(source: ManifestSource, content: bytes) -> None:
    if not 100 <= len(content) <= MAX_SOURCE_BYTES:
        raise ValueError(f"{source.source_id}: source size is outside 100 B–25 MB")
    if source.source_type == "paper":
        if not content.startswith(b"%PDF"):
            raise ValueError(f"{source.source_id}: response is not a PDF")
    elif content.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError(f"{source.source_id}: response is HTML, not Markdown")
    else:
        content.decode("utf-8-sig")


async def download(source: ManifestSource, destination: Path) -> dict[str, object]:
    url = source_url(source)
    if destination.exists():
        content = destination.read_bytes()
        validate_content(source, content)
        return {"url": url, "sha256": sha256(content).hexdigest(), "bytes": len(content)}

    async with (
        httpx.AsyncClient(timeout=90, follow_redirects=True) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        if any(
            item.url.scheme != "https" or item.url.host not in ALLOWED_DOWNLOAD_HOSTS
            for item in (*response.history, response)
        ):
            raise ValueError(f"{source.source_id}: unexpected download redirect")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise ValueError(f"{source.source_id}: download exceeds 25 MB")
            chunks.append(chunk)
    content = b"".join(chunks)
    validate_content(source, content)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(destination)
    return {"url": url, "sha256": sha256(content).hexdigest(), "bytes": len(content)}


async def ingest(
    sources: list[ManifestSource], root: Path, manifest_path: Path
) -> list[dict[str, object]]:
    manifest = load_manifest(manifest_path)
    settings = Settings(knowledge_worker_enabled=False)
    container = build_container(settings)
    platform = container.knowledge
    await platform.start()
    results: list[dict[str, object]] = []
    try:
        try:
            selected_space = await platform.get_space(manifest.space)
        except KeyError:
            selected_space = await platform.create_space(
                slug=manifest.space,
                name=manifest.name,
                description=manifest.description,
            )
        for source in sources:
            path = source_path(root, source)
            content = path.read_bytes()
            validate_content(source, content)
            digest = sha256(content).hexdigest()
            existing_document = await platform.repository.find_document(
                selected_space.id, source.source_id
            )
            recovery_job = None
            if existing_document is not None:
                existing_versions = await platform.repository.list_versions(existing_document.id)
                same_content = next(
                    (version for version in existing_versions if version.content_hash == digest),
                    None,
                )
                if same_content is not None:
                    if (
                        same_content.status == VersionStatus.READY
                        and existing_document.current_version_id == same_content.id
                    ):
                        result = {
                            "source_id": source.source_id,
                            "document_id": existing_document.id,
                            "version_id": same_content.id,
                            "status": "already_indexed",
                            "sha256": digest,
                            "bytes": len(content),
                        }
                        results.append(result)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        continue
                    if same_content.status != VersionStatus.FAILED:
                        raise RuntimeError(
                            f"{source.source_id}: same content has an unpublished version; "
                            "inspect its ingestion job before continuing"
                        )
                    jobs = await platform.list_jobs(manifest.space)
                    recovery_job = next(
                        (
                            item
                            for item in jobs
                            if item.version_id == same_content.id
                            and item.status
                            in {JobStatus.FAILED_TERMINAL, JobStatus.FAILED_RETRYABLE}
                        ),
                        None,
                    )
                    if recovery_job is None:
                        raise RuntimeError(f"{source.source_id}: no failed job to retry")
            if recovery_job is not None:
                job = await platform.retry_job(recovery_job.id)
            else:
                job = await platform.submit(
                    space=manifest.space,
                    title=source.title,
                    content=content,
                    mime_type=(
                        "application/pdf" if source.source_type == "paper" else "text/markdown"
                    ),
                    source_id=source.source_id,
                    canonical_uri=source.canonical_uri,
                    metadata={
                        "topic": source.topic,
                        "source_type": source.source_type,
                        "language": source.language,
                        "tags": [source.topic],
                        "priority": source.priority,
                    },
                    rights={
                        "redistribution_policy": source.ingestion_policy,
                        "license_spdx_id": source.license_spdx_id,
                        "license_evidence_uri": source.license_evidence_uri,
                    },
                    provenance={
                        "retrieval_method": "explicit_manifest_import",
                        "download_url": source_url(source),
                        "content_sha256": digest,
                        "imported_at": datetime.now(UTC).isoformat(),
                    },
                )
            if job.status != JobStatus.COMPLETED:
                claimed = await platform.repository.claim_job(job.id)
                if claimed is not None:
                    job = await platform.process_job(claimed.id)
                else:
                    # The API worker claimed this job first. Never process it
                    # twice; wait for the published outcome instead.
                    deadline = asyncio.get_running_loop().time() + 600
                    while asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(0.5)
                        job = await platform.get_job(job.id)
                        if job.status in {
                            JobStatus.COMPLETED,
                            JobStatus.FAILED_TERMINAL,
                            JobStatus.CANCELLED,
                        }:
                            break
                    else:
                        raise TimeoutError(f"{source.source_id}: ingestion job timed out")
            document, versions = await platform.get_document(job.document_id)
            version = next((item for item in versions if item.id == job.version_id), None)
            if (
                job.status != JobStatus.COMPLETED
                or version is None
                or version.status != VersionStatus.READY
                or document.current_version_id != version.id
            ):
                raise RuntimeError(
                    f"{source.source_id}: import did not publish a ready version: "
                    f"{job.status}, {job.error_code}, {job.error_message}"
                )
            result = {
                "source_id": source.source_id,
                "document_id": document.id,
                "version_id": version.id,
                "job_id": job.id,
                "status": str(job.status),
                "sha256": digest,
                "bytes": len(content),
            }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        await platform.close()
        await container.store.close()
    return results


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=PROJECT_ROOT / "corpus_manifests/llm-engineering-v1.yaml"
    )
    parser.add_argument(
        "--root", type=Path, default=PROJECT_ROOT / "data/knowledge/corpus/llm-engineering-v1"
    )
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument(
        "--download", action="store_true", help="Fetch official originals to E: only"
    )
    parser.add_argument("--ingest", action="store_true", help="Index local originals in PostgreSQL")
    args = parser.parse_args()
    if not args.download and not args.ingest:
        parser.error("specify --download and/or --ingest")
    root = args.root.resolve()
    if root.drive.upper() != "E:":
        parser.error("corpus root must be on E:")
    manifest = load_manifest(args.manifest)
    selected_ids = list(dict.fromkeys(args.sources or DEFAULT_SOURCES))
    known = {source.source_id: source for source in manifest.sources}
    unknown = set(selected_ids) - known.keys()
    if unknown:
        parser.error(f"unknown source IDs: {', '.join(sorted(unknown))}")
    sources = [known[source_id] for source_id in selected_ids]
    if args.download:
        for source in sources:
            result = await download(source, source_path(root, source))
            print(
                json.dumps({"source_id": source.source_id, **result}, ensure_ascii=False),
                flush=True,
            )
    if args.ingest:
        results = await ingest(sources, root, args.manifest)
        ledger = {
            "manifest": str(args.manifest.resolve()),
            "space": manifest.space,
            "generated_at": datetime.now(UTC).isoformat(),
            "documents": results,
        }
        root.mkdir(parents=True, exist_ok=True)
        (root / "import-ledger.json").write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
