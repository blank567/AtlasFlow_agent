"""Preview or stage a whole-space rebuild, then explicitly publish it.

Preview and --verify only read the API. --enqueue creates an isolated build for
all active documents; workers call Embedding but keep the old space published.
--publish switches the whole space atomically after every job completes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import quote

import httpx


def show(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


async def run(
    *, base_url: str, space: str, action: str, build_id: str | None, note: str
) -> int:
    path = f"/api/v1/knowledge/spaces/{quote(space, safe='')}"
    async with httpx.AsyncClient(base_url=base_url, timeout=120) as client:
        if action == "enqueue":
            response = await client.post(f"{path}/reindex", json={"note": note})
        elif action in {"verify", "publish", "cancel"}:
            build_path = f"{path}/builds/{quote(build_id or '', safe='')}"
            if action == "verify":
                response = await client.get(build_path)
            else:
                response = await client.post(f"{build_path}/{action}")
        else:
            response = await client.get(f"{path}/documents")
            response.raise_for_status()
            documents = response.json()
            active = [item for item in documents if item["status"] == "active"]
            for item in active:
                show({"document_id": item["id"], "title": item["title"], "action": "preview"})
            show({
                "space": space,
                "documents": len(documents),
                "rebuild_documents": len(active),
                "archived_documents": len(documents) - len(active),
                "action": "preview",
            })
            return 0
        response.raise_for_status()
        result = response.json()
        show(result)
        return 1 if action == "verify" and result["status"] not in {"ready", "published"} else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--space", required=True, help="Existing knowledge space slug")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--build", help="Build ID returned by --enqueue")
    parser.add_argument("--note", default="", help="Description for the new whole-space version")
    actions = parser.add_mutually_exclusive_group()
    for action, help_text in {
        "enqueue": "Stage all active documents (workers use Embedding API)",
        "verify": "Read build status; exit 0 only if ready or already published",
        "publish": "Atomically publish a completed whole-space build",
        "cancel": "Cancel a build when no job is running; keep old space published",
    }.items():
        actions.add_argument(f"--{action}", dest="action", action="store_const", const=action, help=help_text)
    parser.set_defaults(action="preview")
    args = parser.parse_args()
    if args.action in {"verify", "publish", "cancel"} and not args.build:
        parser.error("--build is required with --verify, --publish, or --cancel")
    if args.build and args.action not in {"verify", "publish", "cancel"}:
        parser.error("--build is only used with --verify, --publish, or --cancel")
    try:
        return asyncio.run(run(
            base_url=args.base_url,
            space=args.space,
            action=args.action,
            build_id=args.build,
            note=args.note,
        ))
    except httpx.HTTPStatusError as exc:
        show({"error": exc.response.text, "status_code": exc.response.status_code})
        return 1
    except httpx.RequestError as exc:
        show({"error": str(exc), "hint": "Start the backend and verify --base-url"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
