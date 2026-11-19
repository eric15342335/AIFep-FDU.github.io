import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from PIL import Image

from tableqa.agent import AgentConfig, cost_rmb, prepare_materials, run, session_stats, task_prompt
from tableqa.xlsxio import Question

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_tools", ROOT / "agent" / "tools.py")
assert spec and spec.loader
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


def question(**kw):
    base = dict(
        id="1",
        file_name="001.pdf",
        question_type="extract",
        question="Q 001.pdf",
        table_hint="",
        answer_format="number",
    )
    return Question(**(base | kw))


def test_task_prompt_hides_file_name_and_asks_for_answer_tool():
    text = task_prompt(question())
    assert "001.pdf" not in text
    assert "answer 工具" in text


def test_prepare_materials_text_layer(tmp_path):
    out = prepare_materials(ROOT / "data/files/006.pdf", tmp_path)
    assert (out / "page-0001.jpg").is_file() and (out / "page-0001.txt").read_text(encoding="utf-8")
    words = json.loads((out / "words-0001.json").read_text(encoding="utf-8"))
    assert words and all(0 <= v <= 1000 for w in words for v in w["box"])
    assert prepare_materials(ROOT / "data/files/006.pdf", tmp_path) == out  # cached


def test_session_stats(tmp_path):
    lines = [
        {"type": "message", "message": {"role": "assistant", "usage": {"input": 10, "output": 2}}},
        {"type": "message", "message": {"role": "toolResult", "toolName": "zoom"}},
        {"type": "message", "message": {"role": "toolResult", "toolName": "zoom"}},
    ]
    (tmp_path / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    stats = session_stats(tmp_path)
    assert stats["turns"] == 1 and stats["tools"] == {"zoom": 2} and stats["usage"]["input"] == 10


def test_cost_estimate_counts_previous_call_as_cached_prefix(tmp_path):
    turns = [{"input": 5000, "output": 200}, {"input": 7000, "output": 100}]
    lines = [{"message": {"role": "assistant", "usage": u}} for u in turns]
    (tmp_path / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    usage = session_stats(tmp_path)["usage"]
    assert usage["cachedEstimate"] == 5200  # turn 2 resends turn 1's prompt and reply
    cost = cost_rmb(usage)
    assert cost["no_cache"] == round((12000 * 0.8 + 300 * 2.7) / 1e6, 4)
    assert cost["prefix_cache_best_case"] == round((6800 * 0.8 + 5200 * 0.1 + 300 * 2.7) / 1e6, 4)


def test_sandbox_check_uses_baseline_validator():
    assert tools.check("extract", "number", '{"answer": "1,250"}') == "1250"
    with pytest.raises(ValueError):
        tools.check("extract", "json_array", '{"answer": 5}')


def test_sandbox_zoom(tmp_path, monkeypatch):
    page = tmp_path / "page-0001.jpg"
    Image.new("RGB", (1000, 800), "white").save(page)
    monkeypatch.setattr(tools, "Path", lambda p: page if "page-0001" in p else Path(p))
    info = tools.zoom(1, [0, 0, 250, 250], str(tmp_path / "z.png"))
    with Image.open(tmp_path / "z.png") as crop:
        assert max(crop.size) == 1000  # 250x200 crop, capped at 4x
    assert info.startswith("1000x800")
    with pytest.raises(ValueError):
        tools.zoom(1, [10, 10, 12, 12], str(tmp_path / "bad.png"))
    with pytest.raises(ValueError, match="too thin"):  # 1000x8 px: Qwen rejects > 200:1
        tools.zoom(1, [0, 0, 1000, 10], str(tmp_path / "thin.png"))


@pytest.mark.parametrize("name, qid", [("../x", "1"), ("ok", "../../.."), ("ok", "a/b")])
def test_run_rejects_ids_that_would_escape_the_run_dir(tmp_path, name, qid):
    q = question(id=qid)
    with pytest.raises(ValueError, match="unsafe"):
        asyncio.run(run([q], [q], tmp_path, tmp_path, AgentConfig(name=name, model="qwen"), {}))
    assert not any(tmp_path.iterdir())


def test_run_stops_starting_questions_after_repeated_failures(tmp_path, monkeypatch):
    # A broken key or an exhausted quota fails every question; the run must not churn through all 908.
    import tableqa.agent as agent_module

    calls = []

    async def failing_solve(q, doc_dir, run_dir, config):
        calls.append(q.id)
        return {"id": q.id, "status": "no_answer", "answer": "", "error": "HTTP 401"}

    monkeypatch.setattr(agent_module, "solve", failing_solve)
    monkeypatch.setattr(agent_module, "prepare_materials", lambda path, cache: tmp_path)
    monkeypatch.setattr(agent_module, "source_path", lambda files, name: tmp_path / name)
    qs = [question(id=str(i)) for i in range(1, 21)]
    report = asyncio.run(run(qs, qs, tmp_path, tmp_path, AgentConfig(name="t", model="qwen", workers=1), {}))
    assert len(calls) == agent_module.MAX_FAILURES_IN_A_ROW
    assert report["statuses"] == {"no_answer": 8, "not_started": 12}


def test_prompt_hides_every_form_of_the_file_name():
    # 255 questions write "62.pdf" for the file 062.pdf; the model must see neither.
    q = question(file_name="062.pdf", question="请恢复 62.pdf 中 Table 13 的表头行结构。")
    prompt = task_prompt(q)
    assert "62.pdf" not in prompt and "请恢复 该文档 中" in prompt


def test_answer_written_before_the_host_stopped_is_kept(tmp_path):
    # The container wrote answer.json, but the host stopped before saving the record: do not pay again.
    from tableqa.agent import saved_result

    q = question(id="7", question_type="extract", answer_format="string")
    work = tmp_path / "work" / "7"
    work.mkdir(parents=True)
    (work / "answer.json").write_text(json.dumps({"response": json.dumps({"answer": "x"})}), encoding="utf-8")
    assert saved_result(q, tmp_path)["answer"] == "x"
    assert json.loads((tmp_path / "records" / "7.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_run_folder_refuses_answers_from_another_model(tmp_path):
    from tableqa.agent import check_same_model

    check_same_model(tmp_path, AgentConfig(name="t", model="qwen", upstream="localhost"))
    check_same_model(tmp_path, AgentConfig(name="t", model="qwen", upstream="localhost"))
    with pytest.raises(ValueError, match="another --name"):
        check_same_model(tmp_path, AgentConfig(name="t", model="qwen", upstream="dashscope.aliyuncs.com"))
