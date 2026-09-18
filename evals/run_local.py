from __future__ import annotations

import argparse
import asyncio
import json
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
        actual_sources = {item.source_id for item in run.evidence}
        actual_tools = {item.tool_name for item in run.tool_calls}
        source_ok = set(case["required_source_ids"]).issubset(actual_sources)
        tools_ok = set(case["expected_tools"]).issubset(actual_tools)
        passed += int(source_ok and tools_ok)
        print(
            json.dumps(
                {
                    "query": case["query"],
                    "source_ok": source_ok,
                    "tools_ok": tools_ok,
                    "status": run.status.value,
                },
                ensure_ascii=False,
            )
        )

    print(f"\nPassed {passed}/{len(cases)} cases")


if __name__ == "__main__":
    asyncio.run(main())
