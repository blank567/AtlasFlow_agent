"""Evaluation v2 HTTP CLI; never initializes production workers or mutates old results."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PureWindowsPath
from urllib.parse import quote

import httpx


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["datasets", "settings", "preflight", "run", "list", "show", "compare", "export", "cancel", "delete"])
    parser.add_argument("--kind", required=True, choices=["rag", "agent"])
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--plan", type=Path, help="E盘JSON计划文件；真实执行需文件内confirm_live=true")
    parser.add_argument("--experiment")
    parser.add_argument("--baseline")
    parser.add_argument("--candidate")
    parser.add_argument("--scope", default="configuration", choices=["configuration", "retrieval", "index", "agent"])
    parser.add_argument("--format", default="json", choices=["json", "csv"])
    parser.add_argument("--confirm-cancel", action="store_true")
    parser.add_argument("--confirm-delete", action="store_true", help="确认软删除指定实验记录；保留证据，不清理原始数据")
    args = parser.parse_args(argv)
    root = f"{args.server.rstrip('/')}/api/v1/evaluations/{args.kind}"
    method, path, payload = "GET", "", None
    if args.command in {"datasets", "settings"}:
        path = f"/{args.command}"
    elif args.command in {"preflight", "run"}:
        if args.plan is None:
            parser.error("--plan is required")
        if PureWindowsPath(str(args.plan)).drive.upper() == "C:":
            parser.error("plan files must not be on drive C")
        payload = json.loads(args.plan.read_text(encoding="utf-8-sig"))
        if payload.get("kind") != args.kind:
            parser.error("plan kind must match --kind")
        if args.kind == "rag" and (payload.get("mode", "live") != "live" or payload.get("input_refs")):
            parser.error("RAG experiments support live execution only; remove frozen input_refs")
        if args.command == "run" and payload.get("mode", "live" if args.kind == "rag" else "replay") == "live" and not payload.get("confirm_live"):
            parser.error("live execution requires confirm_live=true in the plan")
        method, path = "POST", "/preflight" if args.command == "preflight" else "/experiments"
    elif args.command == "list":
        path = "/experiments"
    elif args.command == "compare":
        if not args.baseline or not args.candidate:
            parser.error("--baseline and --candidate are required")
        method, path = "POST", "/compare"
        payload = {"baseline_id": args.baseline, "candidate_id": args.candidate, "scope": args.scope}
    else:
        if not args.experiment:
            parser.error("--experiment is required")
        path = f"/experiments/{quote(args.experiment, safe='')}"
        if args.command == "export":
            path += f"/export?format={args.format}"
        elif args.command == "cancel":
            if not args.confirm_cancel:
                parser.error("cancel requires --confirm-cancel")
            method, path = "POST", path + "/cancel"
        elif args.command == "delete":
            if not args.confirm_delete:
                parser.error("delete requires --confirm-delete")
            method = "DELETE"
    try:
        with httpx.Client(timeout=30) as client:
            response = client.request(method, root + path, json=payload)
            response.raise_for_status()
        if args.command == "export":
            print(response.text)
        else:
            value = response.json()
            print(json.dumps(value, ensure_ascii=False, indent=2))
            if args.command == "preflight" and not value.get("ready"):
                return 2
    except (httpx.HTTPError, ValueError) as exc:
        print(f"evaluation v2 request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
