"""The runner of the agent harness. Each question gets its own Docker container with a pi agent inside.

This code runs on the host machine, not inside the containers. It prepares the material for each
document, starts one container per question, checks every answer again with the baseline's
`normalize_response`, and writes `submission.xlsx` and a cost report. With `--fallback`, it can fill
the questions the agent did not answer from another submission file.
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
# qwen3.8-flash list price on Bailian (Beijing), RMB per million tokens, read 2026-11-19.
# Same for thinking and non-thinking; output includes the reasoning tokens. The implicit-cache
# price is not published in RMB: 0.1 follows the USD ratio (0.014 / 0.113 of input).
PRICE_RMB = {"input": 0.8, "cached_input": 0.1, "output": 2.7}
SUBMIT_NOTE = (
    "提交方式：不要直接输出 JSON，而是调用 answer 工具。answer 参数填写上述 JSON 中 answer 字段的值；"
    "推理题把 calculations 数组填入 calculations 参数。"
)
# Stop starting new questions after this many failures in a row. That many usually means the API key,
# the quota or the service is broken, and every further question would fail the same way.
MAX_FAILURES_IN_A_ROW = 8
NO_TEXT_NOTE = "本文档没有 PDF 文本层（没有 page-*.txt 或 words-*.json），只能用 zoom 读图。"
# Raise this whenever prepare_materials changes what it writes, so old prepared folders are not reused.
MATERIALS_VERSION = 2


@dataclass(frozen=True)
class AgentConfig:
    name: str  # the run name, which is also the name of the output folder
    model: str
    image: str = "tableqa-agent:dev"
    workers: int = 16
    timeout: int = 1800
    max_turns: int = 25
    thinking: str = (
        "high"  # pi's thinking level; agent/pi/models.json turns "high" into reasoning_effort=xhigh
    )
    resume: bool = False  # give old turn-capped sessions their final answer-only turn; start nothing new
    upstream: str = ""  # host name of the model server; a run folder never mixes answers from two servers


def readable(text: str) -> bool:
    """False for a text layer whose fonts map glyphs to the wrong characters.

    Such a layer reads like '!"#$%&()*+' instead of words. In this data set, real text layers are 79-96%
    letters, digits and CJK characters, and broken ones 13-29%.
    """
    chars = [c for c in text if not c.isspace()]
    return sum(c.isalnum() for c in chars) >= 0.5 * len(chars)


def prepare_materials(path: Path, cache_dir: Path) -> Path:
    """Render the page images and extract the PDF text layer, once per document.

    The page images come from the same renderer as the baseline.
    """
    document = prepare_document(path, cache_dir / "pages")
    out = cache_dir / "docs" / f"v{MATERIALS_VERSION}-{document.sha256}"
    if (out / "done").is_file():
        return out
    out.mkdir(parents=True, exist_ok=True)
    for number, image in enumerate(document.images, 1):
        shutil.copy2(image, out / f"page-{number:04d}.jpg")
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as pdf:
            for number, page in enumerate(pdf, 1):
                text = page.get_text().strip()
                # A broken text layer would mislead the agent, so skip it; the agent zooms instead.
                if not text or not readable(text):
                    continue
                (out / f"page-{number:04d}.txt").write_text(text, encoding="utf-8")
                # Word positions on the rendered (already rotated) page, scaled to 0-1000 like `zoom`.
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


def task_prompt(question: Question, has_text_layer: bool = True) -> str:
    # In the pilot run, agents without this note first searched for a text layer that did not exist.
    note = "" if has_text_layer else "\n" + NO_TEXT_NOTE
    return question_prompt(question) + "\n" + SUBMIT_NOTE + note


def read_answers(path: Path) -> dict[str, str]:
    return {id_key(row["id"]): str(row.get("answer") or "") for row in read_rows(path, {"id", "answer"})}


def capped_session(work: Path) -> Path | None:
    """Prepare a saved session that the old hard turn limit cut off, so it can get its final turn.

    The session is trimmed so that it ends on its last tool result. The original file is kept next to it
    as `*.capped` (not `*.jsonl`, so the statistics do not count it twice).
    """
    files = list((work / "session").glob("*.jsonl"))
    if len(files) != 1:
        return None
    lines = files[0].read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1]).get("message") or {} if lines else {}
    if last.get("role") != "assistant" or last.get("stopReason") != "aborted" or last.get("content"):
        return None
    files[0].rename(files[0].with_suffix(".capped"))
    files[0].write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    return files[0]


def docker_command(
    question: Question,
    config: AgentConfig,
    doc_dir: Path,
    task_dir: Path,
    work: Path,
    name: str,
    resume: Path | None = None,
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
        *(["-e", f"AGENT_RESUME=/work/session/{resume.name}"] if resume else []),
        config.image,
    ]


def saved_result(question: Question, run_dir: Path) -> dict[str, Any] | None:
    """The answer an earlier invocation of this run already paid for, or None."""
    key = id_key(question.id)
    record_path = run_dir / "records" / f"{key}.json"
    if record_path.is_file():
        cached = json.loads(record_path.read_text(encoding="utf-8"))
        if cached.get("status") in ("ok", "empty"):
            return {**cached, "cached": True}
    # The container may have written its answer just before the host was stopped, so no record exists.
    work = run_dir / "work" / key
    if (work / "answer.json").is_file():
        result: dict[str, Any] = {"id": question.id, "question_type": question.question_type, "answer": ""}
        result.update(status="failed", cached=True, recovered=True)
        collect_answer(question, work, result, "")
        if result["status"] in ("ok", "empty"):
            result.update(session_stats(work / "session"))
            atomic_json(record_path, result)
            return result
    return None


async def solve(question: Question, doc_dir: Path, run_dir: Path, config: AgentConfig) -> dict[str, Any]:
    key = id_key(question.id)
    record_path = run_dir / "records" / f"{key}.json"
    task_dir, work = run_dir / "tasks" / key, run_dir / "work" / key
    resume = None
    if config.resume:
        resume = capped_session(work)
        if resume is None:
            return {"id": question.id, "status": "skipped", "answer": "", "cached": True}
    else:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        work.chmod(0o777)  # On Linux the container's user may differ from the host user that made it.
    task_dir.mkdir(parents=True, exist_ok=True)
    prompt = task_prompt(question, has_text_layer=any(doc_dir.glob("page-*.txt")))
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
    # Write the container's output to files, not memory: the container decides how much it prints.
    log = "-resume" if resume else ""
    with (work / f"stdout{log}.txt").open("wb") as stdout, (work / f"stderr{log}.txt").open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            *docker_command(question, config, doc_dir, task_dir, work, name, resume),
            stdout=stdout,
            stderr=stderr,
        )
        try:
            result["exit_code"] = await asyncio.wait_for(process.wait(), config.timeout)
        except TimeoutError:
            subprocess.run(["docker", "kill", name], capture_output=True, check=False)
            await process.wait()
            result["status"] = "timeout"
    result["seconds"] = round(time.monotonic() - started, 1)
    collect_answer(question, work, result, log)
    result.update(session_stats(work / "session"))
    atomic_json(record_path, result)
    return result


def collect_answer(question: Question, work: Path, result: dict[str, Any], log: str) -> None:
    """Read the container's answer.json, validate it again on the host, and set the answer and status."""
    answer_file = work / "answer.json"
    if answer_file.is_file():
        try:  # Never trust what comes out of a container.
            submitted = json.loads(answer_file.read_text(encoding="utf-8"))
            result["answer"] = normalize_response(
                submitted["response"], question.question_type, question.answer_format
            )
            result["status"] = "ok" if result["answer"] else "empty"
        except (ValueError, KeyError, TypeError) as exc:
            result["error"] = f"host validation: {exc}"
    elif result["status"] != "timeout":
        result["status"] = "no_answer"
        result["error"] = (work / f"stderr{log}.txt").read_bytes()[-2000:].decode("utf-8", "replace")


def session_stats(session_dir: Path) -> dict[str, Any]:
    tools: Counter[str] = Counter()
    usage = Counter[str]()
    turns = 0
    for path in session_dir.glob("**/*.jsonl"):
        history = (
            0  # tokens in the previous call's prompt and reply, which the next call repeats at its start
        )
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:  # These are only statistics, so a damaged line must not throw away a validated answer.
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
                # On a call with no reported hit, estimate what an implicit prefix cache could
                # have hit: everything the previous call already sent or received.
                if not counts["cacheRead"]:
                    usage["cachedEstimate"] += min(counts["input"], history)
                history = counts["input"] + counts["cacheRead"] + counts["output"]
    return {"turns": turns, "tools": dict(tools), "usage": dict(usage)}


def cost_rmb(tokens: dict[str, int]) -> dict[str, float]:
    """Official API cost of these tokens: with no cache, with the hits the API reported, best case."""
    hits, output = tokens.get("cacheRead", 0), tokens.get("output", 0)
    prompt = tokens.get("input", 0) + hits

    def cost(cached: int) -> float:
        paid = (prompt - cached) * PRICE_RMB["input"] + cached * PRICE_RMB["cached_input"]
        return round((paid + output * PRICE_RMB["output"]) / 1e6, 4)

    return {
        "no_cache": cost(0),
        "reported_cache_hits": cost(hits),
        "prefix_cache_best_case": cost(hits + tokens.get("cachedEstimate", 0)),
    }


async def solve_safely(
    question: Question, doc_dir: Path, run_dir: Path, config: AgentConfig
) -> dict[str, Any]:
    try:
        return await solve(question, doc_dir, run_dir, config)
    except Exception as exc:  # pylint: disable=broad-exception-caught  # one broken container must not stop the run
        return {"id": question.id, "status": "failed", "answer": "", "error": repr(exc)}


def check_safe_names(run_name: str, selected: list[Question]) -> None:
    # Run names and question ids become folder and container names, so allow only safe characters.
    for value in [run_name, *(id_key(q.id) for q in selected)]:
        if not SAFE_NAME.fullmatch(value):
            raise ValueError(f"unsafe run name or question id: {value!r}")


def check_same_model(run_dir: Path, config: AgentConfig) -> None:
    """Refuse to add answers from a different model or model server to an existing run folder."""
    origin = {"model": config.model, "upstream": config.upstream}
    path = run_dir / "origin.json"
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved != origin:
            raise ValueError(f"{run_dir} holds answers from {saved}, not {origin}; choose another --name")
    elif any((run_dir / "records").glob("*.json")):
        raise ValueError(f"{run_dir} holds answers from an unknown model; choose another --name")
    else:
        atomic_json(path, origin)


class FailureStreak:
    """Counts questions that failed in a row; after MAX_FAILURES_IN_A_ROW, no new questions start."""

    def __init__(self) -> None:
        self.count = 0

    @property
    def tripped(self) -> bool:
        return self.count >= MAX_FAILURES_IN_A_ROW

    def record(self, status: str) -> bool:
        """Count one finished question. Returns True for the failure that trips the limit."""
        if self.tripped:  # Once tripped, stay tripped: a late success must not restart the run.
            return False
        if status in ("ok", "empty"):
            self.count = 0
        elif status != "skipped":
            self.count += 1
            return self.count == MAX_FAILURES_IN_A_ROW
        return False


def summarize(config: AgentConfig, selected: list[Question], results: list[dict[str, Any]]) -> dict[str, Any]:
    usage = sum((Counter(r.get("usage", {})) for r in results), Counter())
    return {
        "run": config.name,
        "config": config.__dict__,
        "selected": len(selected),
        "statuses": dict(Counter(r["status"] for r in results)),
        "tool_calls": dict(sum((Counter(r.get("tools", {})) for r in results), Counter())),
        "mean_turns": round(sum(r.get("turns", 0) for r in results) / max(1, len(results)), 2),
        "tokens": dict(usage),
        "official_api_cost_rmb": cost_rmb(usage),
        "sandbox_seconds": round(sum(r.get("seconds", 0) for r in results), 1),
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
    check_safe_names(config.name, selected)
    run_dir = out_dir / config.name
    check_same_model(run_dir, config)
    docs = {
        name: prepare_materials(source_path(files_dir, name), out_dir / "cache")
        for name in dict.fromkeys(q.file_name for q in selected)
    }
    gate = asyncio.Semaphore(config.workers)
    results: list[dict[str, Any]] = []
    streak = FailureStreak()

    async def one(question: Question) -> None:
        async with gate:
            saved = saved_result(question, run_dir)
            if saved is not None:
                result: dict[str, Any] = saved
            elif streak.tripped:
                result = {"id": question.id, "status": "not_started", "answer": ""}
            else:
                result = await solve_safely(question, docs[question.file_name], run_dir, config)
                if streak.record(result["status"]):
                    progress(
                        f"{MAX_FAILURES_IN_A_ROW} questions failed in a row; starting no new questions. "
                        f"Last error: {str(result.get('error', ''))[-500:]}"
                    )
        results.append(result)
        progress(
            f"[{len(results)}/{len(selected)}] id={question.id} {result['status']} "
            f"turns={result.get('turns', '-')} tools={result.get('tools', {})}"
        )

    await asyncio.gather(*(one(q) for q in selected))

    # Take every answer this run folder holds, not only this invocation's, so that rerunning a few
    # questions with --ids does not empty the rest of the submission.
    answers = {}
    for question in questions:
        saved = saved_result(question, run_dir)
        if saved and saved.get("answer"):
            answers[id_key(question.id)] = saved["answer"]
    filled = {k: v for k, v in fallback.items() if k not in answers and v}
    write_submission(run_dir / "submission.xlsx", questions, answers | filled)
    report = summarize(config, selected, results)
    report.update(answers_from_agent=len(answers), answers_from_fallback=len(filled))
    atomic_json(run_dir / "report.json", report)
    return report
