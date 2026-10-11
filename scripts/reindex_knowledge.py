"""Preview or explicitly enqueue reindexing for legacy documents in one space.

The backend API must be running. Preview is the default and performs only GETs;
--enqueue creates new versioned jobs, which the backend worker later processes.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx

CURRENT_PARSER_VERSION = "structured-parser-v2"
CURRENT_CHUNKER_VERSION = "parent-child-v2"


async def run(*, base_url: str, space: str, enqueue: bool) -> int:
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        response = await client.get(f"/api/v1/knowledge/spaces/{space}/documents")
        response.raise_for_status()
        documents = response.json()
        candidates = 0
        for listed in documents:
            response = await client.get(f"/api/v1/knowledge/documents/{listed['id']}")
            response.raise_for_status()
            detail = response.json()
            document = detail["document"]
            current = next(
                (
                    item for item in detail["versions"]
                    if item["id"] == document["current_version_id"]
                ),
                None,
            )
            if document["status"] != "active" or not current or current["status"] != "ready":
                continue
            if (
                current["parser_version"] == CURRENT_PARSER_VERSION
                and current["chunker_version"] == CURRENT_CHUNKER_VERSION
            ):
                continue
            candidates += 1
            result: dict[str, object] = {
                "document_id": document["id"],
                "title": document["title"],
                "from_parser": current["parser_version"],
                "from_chunker": current["chunker_version"],
                "action": "preview",
            }
            if enqueue:
                queued = await client.post(
                    f"/api/v1/knowledge/documents/{document['id']}/reindex"
                )
                queued.raise_for_status()
                job = queued.json()
                result.update({"action": "enqueued", "job_id": job["id"]})
            print(json.dumps(result, ensure_ascii=False), flush=True)
        print(
            json.dumps(
                {"space": space, "candidates": candidates, "enqueued": enqueue},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--space", required=True, help="Existing knowledge space slug")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--enqueue", action="store_true", help="Create versioned reindex jobs (uses Embedding API)"
    )
    args = parser.parse_args()
    return asyncio.run(run(base_url=args.base_url, space=args.space, enqueue=args.enqueue))


if __name__ == "__main__":
    raise SystemExit(main())
