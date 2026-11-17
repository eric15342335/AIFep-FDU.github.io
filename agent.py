#!/usr/bin/env python3
"""CLI for the agentic harness: one pi agent in an isolated Docker sandbox per question."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from tableqa import agent, smoke
from tableqa.schema import json_loads
from tableqa.xlsxio import id_key, read_questions

GATEWAY = "http://127.0.0.1:4000/v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=Path, default=Path("data/tests.xlsx"))
    parser.add_argument("--dataset-fixes", type=Path, default=Path("configs/downloaded_data_fixes.json"))
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("smoke", help="check the endpoint supports everything the harness needs")
    check.add_argument("--base-url", default=GATEWAY)
    check.add_argument("--page", type=Path, default=Path("data/files/010.png"))

    run = sub.add_parser("run", help="answer the questions with the agent")
    run.add_argument("--name", default="agent", help="run name; output goes to <output-dir>/<name>")
    run.add_argument("--files", type=Path, default=Path("data/files"))
    run.add_argument("--output-dir", type=Path, default=Path("runs"))
    run.add_argument("--fallback", type=Path, help="submission whose answers fill questions the agent missed")
    run.add_argument("--image", default="tableqa-agent:dev")
    run.add_argument("--workers", type=int, default=16)
    run.add_argument("--timeout", type=int, default=1800, help="seconds per question")
    run.add_argument("--max-turns", type=int, default=25)
    run.add_argument(
        "--thinking", choices=["off", "high"], default="high", help="high = Qwen reasoning_effort xhigh (max)"
    )
    run.add_argument("--limit", type=int, help="first N questions only")
    run.add_argument("--ids", help="comma-separated question ids")

    args = parser.parse_args(argv)
    model = os.getenv("AGENT_MODEL", "")
    if not model:
        parser.error("set AGENT_MODEL (source .env)")

    if args.command == "smoke":
        results = smoke.run(args.base_url, model, args.page)
        for name, ok, detail in results:
            print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
        return 0 if all(ok for name, ok, _ in results if "info only" not in name) else 1

    fixes = json_loads(args.dataset_fixes.read_text(encoding="utf-8"))
    questions = read_questions(args.tests, None, fixes)
    selected = questions
    if args.ids:
        wanted = set(args.ids.split(","))
        selected = [q for q in questions if id_key(q.id) in wanted]
    if args.limit:
        selected = selected[: args.limit]
    fallback = agent.read_answers(args.fallback) if args.fallback else {}
    config = agent.AgentConfig(
        name=args.name,
        model=model,
        image=args.image,
        workers=args.workers,
        timeout=args.timeout,
        max_turns=args.max_turns,
        thinking=args.thinking,
    )
    report = asyncio.run(agent.run(questions, selected, args.files, args.output_dir, config, fallback))
    report["fallback"] = str(args.fallback) if args.fallback else None
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
