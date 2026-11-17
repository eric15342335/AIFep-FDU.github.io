"""Agentic harness runner: one isolated Docker sandbox (pi agent) per question.

Host side only: prepares per-document materials, starts sandboxes, re-validates answers with the
baseline's `normalize_response`, and writes submission.xlsx plus a cost report. Optionally fills
questions the agent could not answer from another submission (`--fallback`).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from .documents import prepare_document, source_path
from .pipeline import atomic_json
from .prompts import question_prompt
from .schema import normalize_response
from .xlsxio import Question, id_key, read_rows, write_submission

SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
NETWORK = "tableqa-sandbox"
# qwen3.8-flash list price on Bailian (Beijing), RMB per million tokens, read 2026-11-17.
# Same for thinking and non-thinking; output includes the reasoning tokens.
PRICE_RMB = {"input": 0.8, "cached_input": 0.1, "output": 2.7}
SUBMIT_NOTE = (
    "提交方式：不要直接输出 JSON，而是调用 answer 工具。answer 参数填写上述 JSON 中 answer 字段的值；"
    "推理题把 calculations 数组填入 calculations 参数。"
)
NO_TEXT_NOTE = "本文档没有 PDF 文本层（没有 page-*.txt 或 words-*.json），只能用 zoom 读图。"


@dataclass(frozen=True)
class AgentConfig:
    name: str  # run name; also the output folder
    model: str
    image: str = "tableqa-agent:dev"
    workers: int = 16
    timeout: int = 1800
    max_turns: int = 25
    thinking: str = "high"  # pi level; models.json maps it to Qwen reasoning_effort=xhigh


def prepare_materials(path: Path, cache_dir: Path) -> Path:
    """Page images (baseline renderer) plus the PDF text layer, once per document."""
    document = prepare_document(path, cache_dir / "pages")
    out = cache_dir / "docs" / document.sha256
    if (out / "done").is_file():
        return out
    out.mkdir(parents=True, exist_ok=True)
    for number, image in enumerate(document.images, 1):
        shutil.copy2(image, out / f"page-{number:04d}.jpg")
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as pdf:
            for number, page in enumerate(pdf, 1):
                text = page.get_text().strip()
                if not text:
                    continue
                (out / f"page-{number:04d}.txt").write_text(text, encoding="utf-8")
                # Words in the rendered (rotation-applied) frame, normalised to 0-1000 like `zoom`.
                matrix = page.rotation_matrix
                width, height = page.rect.width, page.rect.height
                words = []
                for x0, y0, x1, y1, word, *_ in page.get_text("words"):
                    box = pymupdf.Rect(x0, y0, x1, y1) * matrix
                    words.append(
                        {
                            "text": word,
                            "box": [
                                round(box.x0 / width * 1000),
                                round(box.y0 / height * 1000),
                                round(box.x1 / width * 1000),
                                round(box.y1 / height * 1000),
                            ],
                        }
                    )
                (out / f"words-{number:04d}.json").write_text(
                    json.dumps(words, ensure_ascii=False), encoding="utf-8"
                )
    (out / "done").touch()
    return out


def task_prompt(question: Question) -> str:
    return question_prompt(question) + "\n" + SUBMIT_NOTE


def read_answers(path: Path) -> dict[str, str]:
    return {id_key(row["id"]): str(row.get("answer") or "") for row in read_rows(path, {"id", "answer"})}


def docker_command(
    question: Question, config: AgentConfig, doc_dir: Path, task_dir: Path, work: Path, name: str
) -> list[str]:
    key = id_key(question.id)
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        NETWORK,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=512m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        "1",
        "--memory",
        "2g",
        "--pids-limit",
        "256",
        "-v",
        f"{doc_dir.resolve()}:/task/doc:ro",
        "-v",
        f"{task_dir.resolve()}:/task/q:ro",
        "-v",
        f"{work.resolve()}:/work",
        "-e",
        f"AGENT_MODEL={config.model}",
        "-e",
        f"AGENT_THINKING={config.thinking}",
        "-e",
        f"AGENT_MAX_TURNS={config.max_turns}",
        "-e",
        f"TABLEQA_TAG=run:{config.name},q:{key}",
        config.image,
    ]


async def solve(question: Question, doc_dir: Path, run_dir: Path, config: AgentConfig) -> dict[str, Any]:
    key = id_key(question.id)
    record_path = run_dir / "records" / f"{key}.json"
    if record_path.is_file():
        cached = json.loads(record_path.read_text(encoding="utf-8"))
        if cached.get("status") in ("ok", "empty"):
            return {**cached, "cached": True}
    task_dir, work = run_dir / "tasks" / key, run_dir / "work" / key
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    task_dir.mkdir(parents=True, exist_ok=True)
    prompt = task_prompt(question)
    if not any(doc_dir.glob("page-*.txt")):  # pilot: agents grepped a missing text layer first
        prompt += "\n" + NO_TEXT_NOTE
    (task_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    (task_dir / "question.json").write_text(
        json.dumps({"question_type": question.question_type, "answer_format": question.answer_format}),
        encoding="utf-8",
    )

    name = f"tq-{config.name}-{key}".lower()
    result: dict[str, Any] = {
        "id": question.id,
        "question_type": question.question_type,
        "answer": "",
        "status": "failed",
        "cached": False,
    }
    started = time.monotonic()
    # Stream to files, not host memory: the sandbox controls how much it prints.
    with (work / "stdout.txt").open("wb") as stdout, (work / "stderr.txt").open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            *docker_command(question, config, doc_dir, task_dir, work, name), stdout=stdout, stderr=stderr
        )
        try:
            result["exit_code"] = await asyncio.wait_for(process.wait(), config.timeout)
        except TimeoutError:
            subprocess.run(["docker", "kill", name], capture_output=True, check=False)
            await process.wait()
            result["status"] = "timeout"
    result["seconds"] = round(time.monotonic() - started, 1)

    answer_file = work / "answer.json"
    if answer_file.is_file():
        submitted = json.loads(answer_file.read_text(encoding="utf-8"))
        try:  # Never trust the sandbox: re-validate on the host.
            result["answer"] = normalize_response(
                submitted["response"], question.question_type, question.answer_format
            )
            result["status"] = "ok" if result["answer"] else "empty"
        except (ValueError, KeyError, TypeError) as exc:
            result["error"] = f"host validation: {exc}"
    elif result["status"] != "timeout":
        result["status"] = "no_answer"
        result["error"] = (work / "stderr.txt").read_bytes()[-2000:].decode("utf-8", "replace")
    result.update(session_stats(work / "session"))
    atomic_json(record_path, result)
    return result


def session_stats(session_dir: Path) -> dict[str, Any]:
    tools: Counter[str] = Counter()
    usage = Counter[str]()
    turns = 0
    for path in session_dir.glob("**/*.jsonl"):
        history = 0  # tokens of the previous call's prompt + reply: the next call's shared prefix
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:  # stats only: a truncated line must not cost the validated answer
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            message = entry.get("message") or {}
            if message.get("role") == "toolResult":
                tools[message.get("toolName", "?")] += 1
            elif message.get("role") == "assistant":
                turns += 1
                raw = message.get("usage") or {}
                counts = {f: int(raw.get(f) or 0) for f in ("input", "output", "cacheRead")}
                usage.update(counts)
                # The server reports no cache hits, so estimate what the official API's implicit
                # prefix cache could hit: everything the previous call already sent or received.
                if not counts["cacheRead"]:
                    usage["cachedEstimate"] += min(counts["input"], history)
                history = counts["input"] + counts["cacheRead"] + counts["output"]
    return {"turns": turns, "tools": dict(tools), "usage": dict(usage)}


def cost_rmb(tokens: dict[str, int]) -> dict[str, float]:
    """What these tokens would cost on the official API, without and with prefix-cache hits."""
    prompt, output = tokens.get("input", 0) + tokens.get("cacheRead", 0), tokens.get("output", 0)
    cached = tokens.get("cacheRead", 0) + tokens.get("cachedEstimate", 0)
    with_cache = (prompt - cached) * PRICE_RMB["input"] + cached * PRICE_RMB["cached_input"]
    return {
        "no_cache": round((prompt * PRICE_RMB["input"] + output * PRICE_RMB["output"]) / 1e6, 4),
        "prefix_cache_best_case": round((with_cache + output * PRICE_RMB["output"]) / 1e6, 4),
    }


async def run(
    questions: list[Question],
    selected: list[Question],
    files_dir: Path,
    out_dir: Path,
    config: AgentConfig,
    fallback: dict[str, str],
    progress=print,
) -> dict[str, Any]:
    # Names and ids become paths and container names: keep them to safe characters.
    for value in [config.name, *(id_key(q.id) for q in selected)]:
        if not SAFE_NAME.fullmatch(value):
            raise ValueError(f"unsafe run name or question id: {value!r}")
    run_dir = out_dir / config.name
    docs = {
        name: prepare_materials(source_path(files_dir, name), out_dir / "cache")
        for name in dict.fromkeys(q.file_name for q in selected)
    }
    gate = asyncio.Semaphore(config.workers)
    results: list[dict[str, Any]] = []

    async def one(question: Question) -> None:
        async with gate:
            try:
                result = await solve(question, docs[question.file_name], run_dir, config)
            except Exception as exc:  # one broken sandbox must not stop the run
                result = {"id": question.id, "status": "failed", "answer": "", "error": repr(exc)}
        results.append(result)
        progress(
            f"[{len(results)}/{len(selected)}] id={question.id} {result['status']} "
            f"turns={result.get('turns', '-')} tools={result.get('tools', {})}"
        )

    await asyncio.gather(*(one(q) for q in selected))

    answers = {id_key(r["id"]): r["answer"] for r in results if r.get("answer")}
    filled = {k: v for k, v in fallback.items() if k not in answers and v}
    write_submission(run_dir / "submission.xlsx", questions, answers | filled)
    usage = sum((Counter(r.get("usage", {})) for r in results), Counter())
    report = {
        "run": config.name,
        "config": config.__dict__,
        "selected": len(selected),
        "statuses": dict(Counter(r["status"] for r in results)),
        "answers_from_agent": len(answers),
        "answers_from_fallback": len(filled),
        "tool_calls": dict(sum((Counter(r.get("tools", {})) for r in results), Counter())),
        "mean_turns": round(sum(r.get("turns", 0) for r in results) / max(1, len(results)), 2),
        "tokens": dict(usage),
        "official_api_cost_rmb": cost_rmb(usage),
        "sandbox_seconds": round(sum(r.get("seconds", 0) for r in results), 1),
    }
    atomic_json(run_dir / "report.json", report)
    return report
