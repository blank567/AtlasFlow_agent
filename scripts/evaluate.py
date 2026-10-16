"""Evaluation CLI. Replay uses only local frozen artifacts; live uses the running API."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path, PureWindowsPath

from atlasflow.evaluation.contracts import (
    EvaluationBudgetSpec,
    EvaluationPlan,
    all_rag_arms,
    default_arms,
)
from atlasflow.evaluation.dataset import DatasetRegistry
from atlasflow.evaluation.runner import EvaluationService
from atlasflow.evaluation.store import EvaluationStore

ROOT = Path(__file__).resolve().parents[1]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "run", "compare", "export"])
    parser.add_argument("--dataset", default="llm-rag-v1")
    parser.add_argument("--mode", choices=["replay", "live"], default="replay")
    parser.add_argument(
        "--case", action="append", default=[], help="Case id; repeat to select multiple"
    )
    parser.add_argument(
        "--all-cases",
        action="store_true",
        help="Select the entire dataset, not the default first ten",
    )
    parser.add_argument(
        "--all-arms",
        action="store_true",
        help="Select all six RAG arms instead of baseline/candidate",
    )
    parser.add_argument(
        "--inputs-file", type=Path, help="JSON case_id:arm_id -> registered frozen input id"
    )
    parser.add_argument("--space")
    parser.add_argument("--space-version-id")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument(
        "--judge", action="store_true", help="Explicit independent Judge network requests"
    )
    parser.add_argument("--confirm-judge", action="store_true")
    parser.add_argument(
        "--sync-langsmith", action="store_true", help="Metadata-only sync through running server"
    )
    parser.add_argument("--max-http-attempts", type=int, default=60)
    parser.add_argument("--max-seconds", type=int, default=1200)
    parser.add_argument("--case-timeout", type=int, default=90)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--store", type=Path, default=ROOT / "data/evaluations/atlasflow-evaluations.sqlite3"
    )
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--baseline")
    parser.add_argument("--candidate")
    parser.add_argument("--comparison-scope", choices=["retrieval", "index"], default="retrieval")
    parser.add_argument("--experiment")
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    return parser.parse_args()


async def server_command(arguments, plan):
    import httpx

    async with httpx.AsyncClient(base_url=arguments.server.rstrip("/"), timeout=30) as client:
        check = await client.post(
            "/api/v1/evaluations/preflight", json=plan.model_dump(mode="json")
        )
        check.raise_for_status()
        preflight = check.json()
        if arguments.command == "preflight" or not preflight["ready"]:
            print(json.dumps(preflight, ensure_ascii=False, indent=2))
            return 0 if preflight["ready"] else 2
        response = await client.post("/api/v1/evaluations", json=plan.model_dump(mode="json"))
        response.raise_for_status()
        experiment = response.json()
        try:
            while experiment["status"] in {"planned", "running"}:
                await asyncio.sleep(1)
                response = await client.get(f"/api/v1/evaluations/{experiment['experiment_id']}")
                response.raise_for_status()
                experiment = response.json()
        except asyncio.CancelledError:
            try:
                await client.post(f"/api/v1/evaluations/{experiment['experiment_id']}/cancel")
            except httpx.HTTPError:
                print(
                    "Client stopped; remote cancellation could not be confirmed. Server budgets remain active."
                )
            raise
        print(json.dumps(experiment, ensure_ascii=False, indent=2))
        return (
            0
            if experiment["status"] == "completed"
            else (3 if experiment["status"] == "partial" else 4)
        )


async def main(arguments=None):
    args = arguments or parse_args()
    for path in (args.store, args.inputs_file):
        if path and PureWindowsPath(str(path)).drive.upper() == "C:":
            raise ValueError("evaluation inputs and output must stay on E")
    if args.command in {"preflight", "run"}:
        refs = json.loads(args.inputs_file.read_text(encoding="utf-8")) if args.inputs_file else {}
        selected = (
            [case.case_id for case in DatasetRegistry(root=ROOT).get(args.dataset).cases]
            if args.all_cases
            else args.case
        )
        plan = EvaluationPlan(
            dataset_id=args.dataset,
            mode=args.mode,
            case_ids=selected,
            arms=all_rag_arms() if args.all_arms else default_arms(),
            input_refs=refs,
            space=args.space,
            space_version_id=args.space_version_id,
            confirm_live=args.confirm_live,
            judge_enabled=args.judge,
            confirm_judge=args.confirm_judge,
            langsmith_sync=args.sync_langsmith,
            budget=EvaluationBudgetSpec(
                max_http_attempts=args.max_http_attempts,
                max_elapsed_seconds=args.max_seconds,
                case_timeout_seconds=args.case_timeout,
                concurrency=args.concurrency,
            ),
        )
        # Any explicit remote Judge/sync is handled by the configured server,
        # never by silently loading production credentials in an offline CLI.
        if plan.mode == "live" or plan.judge_enabled or plan.langsmith_sync:
            return await server_command(args, plan)
    service = EvaluationService(EvaluationStore(args.store), DatasetRegistry(root=ROOT))
    await service.initialize()
    try:
        if args.command == "compare":
            if not args.baseline or not args.candidate:
                raise ValueError("compare requires --baseline and --candidate")
            print(
                json.dumps(
                    await service.compare(args.baseline, args.candidate, args.comparison_scope),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "export":
            if not args.experiment:
                raise ValueError("export requires --experiment")
            print(await service.export(args.experiment, args.format))
            return 0
        check = await service.preflight(plan)
        if args.command == "preflight" or not check["ready"]:
            print(json.dumps(check, ensure_ascii=False, indent=2))
            return 0 if check["ready"] else 2
        experiment = await service.create(plan, wait=True)
        print(experiment.model_dump_json(indent=2))
        return (
            0 if experiment.status == "completed" else (3 if experiment.status == "partial" else 4)
        )
    finally:
        await service.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (ValueError, KeyError) as error:
        print(f"evaluation preflight failed: {error}")
        raise SystemExit(2) from error
