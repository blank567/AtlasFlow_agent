from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path

from atlasflow.bootstrap import build_container
from atlasflow.config import Settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run AtlasFlow's live regression dataset against configured providers."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Acknowledge that this command makes real network calls and may incur charges.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if not args.live:
        raise SystemExit(
            "Refusing to call real providers without --live. "
            "Run `python evals/run_local.py --live` after checking API quotas and costs."
        )
    dataset_path = Path(__file__).with_name("dataset.jsonl")
    cases = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    container = build_container(Settings())
    passed = 0

    for case in cases:
        run = await container.run_service.execute_and_wait(case["query"])
        plan_size = len(run.plan.tasks) if run.plan else 0
        status_ok = run.status.value in case["accepted_statuses"]
        plan_ok = case["min_tasks"] <= plan_size <= case["max_tasks"]
        quorum_ok = len(run.research_results) >= math.ceil(
            plan_size * case["min_research_ratio"]
        )
        report_ok = bool(run.report and run.report.strip())
        audit_ok = bool(run.route_history and run.events)
        case_passed = status_ok and plan_ok and quorum_ok and report_ok and audit_ok
        passed += int(case_passed)
        print(
            json.dumps(
                {
                    "query": case["query"],
                    "status": run.status.value,
                    "status_ok": status_ok,
                    "plan_ok": plan_ok,
                    "quorum_ok": quorum_ok,
                    "report_ok": report_ok,
                    "audit_ok": audit_ok,
                    "plan_tasks": plan_size,
                    "research_results": len(run.research_results),
                    "quality_scores": [item.score for item in run.quality_history],
                    "passed": case_passed,
                },
                ensure_ascii=False,
            )
        )

    print(f"\nPassed {passed}/{len(cases)} cases")


if __name__ == "__main__":
    asyncio.run(main())
