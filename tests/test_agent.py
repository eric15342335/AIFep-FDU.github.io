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
