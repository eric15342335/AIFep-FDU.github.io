#!/usr/bin/env python3
"""Command-line entry point for the agent harness.

Each question is answered by one pi agent running in its own isolated Docker container. Both commands
prepare everything themselves: they read `.env`, rebuild the sandbox image (cached, so this takes
seconds when nothing changed) and start the LiteLLM gateway and Phoenix tracing with Docker Compose.

    uv run agent.py smoke                 # check that the model endpoint supports what the harness needs
    uv run agent.py run --limit 10        # answer the first 10 questions
    uv run agent.py run                   # answer all questions; rerunning resumes where it stopped
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from tableqa import agent, smoke
from tableqa.schema import json_loads
from tableqa.xlsxio import id_key, read_questions

ROOT = Path(__file__).resolve().parent
GATEWAY = "http://127.0.0.1:4000/v1"
IMAGE = "tableqa-agent:dev"


def load_env(path: Path) -> None:
    """Read `KEY=value` lines from a .env file. Variables already set in the shell take priority."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def start_services(image: str) -> None:
    """Build the sandbox image and start the gateway, then wait until the gateway answers."""
    # Always rebuild: the image contains the pi configuration, so a stale image would run old settings.
    subprocess.run(
        ["docker", "build", "-q", "-f", "agent/Dockerfile", "-t", image, "."], cwd=ROOT, check=True
    )
    subprocess.run(["docker", "compose", "-f", "agent/compose.yaml", "up", "-d"], cwd=ROOT, check=True)
    deadline = time.monotonic() + 120
    while True:
        try:
            with urlopen(GATEWAY.removesuffix("/v1") + "/health/liveliness", timeout=5):
                return
        except (URLError, OSError):
            if time.monotonic() > deadline:
                raise SystemExit(
                    "The LiteLLM gateway did not start; see `docker compose -f agent/compose.yaml logs`"
                ) from None
            time.sleep(2)


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=Path, default=ROOT / "data/tests.xlsx")
    parser.add_argument("--dataset-fixes", type=Path, default=ROOT / "configs/downloaded_data_fixes.json")
    parser.add_argument(
        "--env-file", type=Path, default=ROOT / ".env", help="API key, base URL and model name"
    )
    parser.add_argument(
        "--no-setup",
        action="store_true",
        help="do not build the image or start the gateway (already running)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("smoke", help="check the endpoint supports everything the harness needs")
    check.add_argument("--base-url", default=GATEWAY)
    check.add_argument("--page", type=Path, default=ROOT / "data/files/010.png")

    run = sub.add_parser("run", help="answer the questions with the agent")
    run.add_argument("--name", default="agent", help="run name; output goes to <output-dir>/<name>")
    run.add_argument("--files", type=Path, default=ROOT / "data/files")
    run.add_argument("--output-dir", type=Path, default=ROOT / "runs")
    run.add_argument("--fallback", type=Path, help="submission whose answers fill questions the agent missed")
    run.add_argument("--image", default=IMAGE)
    run.add_argument("--workers", type=positive, default=16)
    run.add_argument("--timeout", type=positive, default=1800, help="seconds per question")
    run.add_argument("--max-turns", type=positive, default=25)
    run.add_argument(
        "--thinking", choices=["off", "high"], default="high", help="high = Qwen reasoning_effort xhigh (max)"
    )
    run.add_argument("--limit", type=positive, help="first N questions only")
    run.add_argument("--ids", help="comma-separated question ids")
    run.add_argument("--resume", action="store_true", help="give turn-capped sessions their grace turn only")
    return parser


def smoke_command(args: argparse.Namespace, model: str) -> int:
    results = smoke.run(args.base_url, model, args.page)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return 0 if all(ok for name, ok, _ in results if "info only" not in name) else 1


def run_command(args: argparse.Namespace, model: str) -> int:
    fixes = json_loads(args.dataset_fixes.read_text(encoding="utf-8"))
    questions = read_questions(args.tests, None, fixes)
    wanted = set(args.ids.split(",")) if args.ids else None
    selected = [q for q in questions if wanted is None or id_key(q.id) in wanted][: args.limit]
    fallback = agent.read_answers(args.fallback) if args.fallback else {}
    config = agent.AgentConfig(
        name=args.name,
        model=model,
        image=args.image,
        workers=args.workers,
        timeout=args.timeout,
        max_turns=args.max_turns,
        thinking=args.thinking,
        resume=args.resume,
        upstream=urlsplit(os.getenv("DASHSCOPE_BASE_URL", "")).hostname or "",
    )
    report = asyncio.run(agent.run(questions, selected, args.files, args.output_dir, config, fallback))
    report["fallback"] = str(args.fallback) if args.fallback else None
    print(json.dumps(report, ensure_ascii=False, indent=2))
    unfinished = sum(
        n for status, n in report["statuses"].items() if status not in ("ok", "empty", "skipped")
    )
    if unfinished:
        print(f"{unfinished} questions have no answer yet. Run the same command again to retry only those.")
    return 1 if unfinished else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env(args.env_file)
    model = os.getenv("AGENT_MODEL", "")
    if not model:
        parser.error(f"AGENT_MODEL is not set; copy .env.example to {args.env_file} and fill it in")
    base_url = os.getenv("DASHSCOPE_BASE_URL", "")
    if not (urlsplit(base_url).hostname or "").endswith(".aliyuncs.com"):
        # Fine for development, but the competition only accepts answers from Alibaba Cloud's Qwen models.
        print(f"Note: {base_url or 'DASHSCOPE_BASE_URL'} is not an Alibaba Cloud endpoint: for testing only.")
    if not args.no_setup:
        start_services(getattr(args, "image", IMAGE))
    return smoke_command(args, model) if args.command == "smoke" else run_command(args, model)


if __name__ == "__main__":
    sys.exit(main())
