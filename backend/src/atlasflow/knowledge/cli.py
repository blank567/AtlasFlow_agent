from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from atlasflow.knowledge.manifest import load_manifest


async def run_repair(space: str, confirm: bool) -> None:
    # Build only the repository. No default space, worker, models or tracing.
    from atlasflow.config import Settings
    from atlasflow.knowledge.application.legacy_repair import repair_legacy_jobs
    from atlasflow.knowledge.infrastructure.postgres_repository import PostgresKnowledgeRepository

    settings = Settings()
    if settings.knowledge_backend != "postgres":
        raise SystemExit("legacy repair requires the configured PostgreSQL repository")
    repository = PostgresKnowledgeRepository(settings.knowledge_database_url)
    try:
        await repository.initialize()
        result = await repair_legacy_jobs(repository, space, confirm_no_workers=confirm)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="atlasflow-knowledge",
        description="Validate corpus manifests or explicitly repair legacy jobs without model calls.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    repair = commands.add_parser("repair-legacy-jobs", help="Preview unbound legacy RUNNING jobs; no model calls")
    repair.add_argument("--space", required=True)
    repair.add_argument("--confirm-no-workers", action="store_true",
                        help="Confirm old API/workers are stopped, then mark unbound legacy RUNNING jobs failed; sources are retained")
    arguments = parser.parse_args()
    if arguments.command == "repair-legacy-jobs":
        asyncio.run(run_repair(arguments.space, arguments.confirm_no_workers))
        return
    manifest = load_manifest(arguments.manifest)
    policies: dict[str, int] = {}
    for source in manifest.sources:
        policies[source.ingestion_policy] = policies.get(source.ingestion_policy, 0) + 1
    print(
        f"valid manifest: space={manifest.space} sources={len(manifest.sources)} "
        f"auto_download={manifest.auto_download} policies={policies}"
    )


if __name__ == "__main__":
    main()
