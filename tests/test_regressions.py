"""Regression tests: one per failure seen in the pilot and full runs (2026-11-17)."""

import importlib.util
import json
import re
from pathlib import Path

import pytest
from PIL import Image

from tableqa.agent import NO_TEXT_NOTE, capped_session, prepare_materials, task_prompt
from tableqa.xlsxio import Question

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_tools", ROOT / "agent" / "tools.py")
assert spec and spec.loader
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


@pytest.fixture
def page_044(tmp_path, monkeypatch):
    """Full run q419's document: an image-only page the sandbox sees as /task/doc/page-0001.jpg."""
    doc = prepare_materials(ROOT / "data/files/044.jpg", tmp_path)
    page = doc / "page-0001.jpg"
    monkeypatch.setattr(tools, "Path", lambda p: page if "page-0001" in p else Path(p))
    return doc


@pytest.mark.parametrize("box", [[0, 90, 1000, 100], [0, 0, 1000, 20]])
def test_q419_thin_zoom_is_rejected_not_sent(page_044, tmp_path, box):
    # A 1600x6 crop got a 400 from Qwen's image processor (> 200:1), and since it stayed in
    # history every later call failed: no_answer. zoom must refuse with a message instead.
    with pytest.raises(ValueError, match="too thin"):
        tools.zoom(1, box, str(tmp_path / "z.jpg"))


def test_q419_normal_zoom_still_works(page_044, tmp_path):
    info = tools.zoom(1, [0, 0, 1000, 200], str(tmp_path / "z.jpg"))
    with Image.open(tmp_path / "z.jpg") as crop:
        assert crop.format == "JPEG"  # the extension sends it as image/jpeg
        assert max(crop.size) <= 200 * min(crop.size)
    assert "px" in info


def test_image_only_document_gets_no_text_layer_note(page_044):
    # Pilot: 14 of 46 bash calls grepped a page-*.txt that image-only documents never have.
    assert not list(page_044.glob("page-*.txt"))
    q = Question(
        id="419",
        file_name="044.jpg",
        question_type="extract",
        question="Q",
        table_hint="",
        answer_format="string",
    )
    assert NO_TEXT_NOTE in task_prompt(q, has_text_layer=False)
    assert NO_TEXT_NOTE not in task_prompt(q, has_text_layer=True)


def test_array_answer_with_null_calculation_index_is_rejected_with_reason():
    # Pilot: the tool schema allows index=null, but an array answer needs an int index.
    # The sandbox must reject with a reason the agent can act on (it fixed it next turn).
    calculation = {"index": None, "expression": "10805362.40-2715764.99", "decimals": 2, "suffix": ""}
    answer = ["本期提取 10,805,362.40", "本期使用 -2,715,764.99"]
    raw = json.dumps({"answer": answer, "calculations": [calculation]}, ensure_ascii=False)
    with pytest.raises(ValueError, match="index"):
        tools.check("thinking", "json_array", raw)


def test_pi_waits_as_long_as_the_gateway():
    # Pilot over a slow network link: pi's default 300 s idle timeout aborted uploads the gateway
    # (900 s) was still serving, and each retry re-sent megabytes of images.
    settings = json.loads((ROOT / "agent/pi/settings.json").read_text(encoding="utf-8"))
    gateway = (ROOT / "agent/litellm.yaml").read_text(encoding="utf-8")
    timeout = re.search(r"request_timeout:\s*(\d+)", gateway)
    assert timeout and settings["httpIdleTimeoutMs"] >= int(timeout.group(1)) * 1000


def test_gateway_routes_only_the_configured_qwen_model():
    # The sandbox can reach the gateway, so the gateway must not spend the key on any other model.
    gateway = (ROOT / "agent/litellm.yaml").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.findall(r"model_name:\s*(\S+)", gateway) == ["qwen3.8-flash"]
    assert "export AGENT_MODEL='qwen3.8-flash'" in env


def test_turn_cap_gives_one_answer_only_grace_turn_after_the_last_regular_turn():
    # Full run: q687, q689, q699 used all 25 turns and were aborted without answering.
    # The grace turn must start only at the cap, so sessions that answered earlier are unchanged.
    ext = (ROOT / "agent/pi/extensions/tableqa.ts").read_text(encoding="utf-8")
    assert re.search(r"turns === MAX_TURNS\)\s*\{\s*pi\.setActiveTools\(\[\"answer\"\]\)", ext)
    assert "else if (turns > MAX_TURNS) ctx.abort()" in ext
    assert (ROOT / "agent/pi/grace.md").read_text(encoding="utf-8").strip()


def test_capped_session_is_trimmed_to_its_last_tool_result(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    entries = [
        {"type": "message", "message": {"role": "assistant", "stopReason": "toolUse", "content": [{}]}},
        {"type": "message", "message": {"role": "toolResult", "toolName": "bash", "content": [{}]}},
        {"type": "message", "message": {"role": "assistant", "stopReason": "aborted", "content": []}},
    ]
    path = session / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    assert capped_session(tmp_path) == path
    roles = [json.loads(x)["message"]["role"] for x in path.read_text().splitlines()]
    assert roles == ["assistant", "toolResult"]
    assert len((session / "s.capped").read_text().splitlines()) == 3  # original kept for audit
    assert capped_session(tmp_path) is None  # already trimmed: nothing to resume twice


def test_answer_wrapped_in_its_own_output_object_is_unwrapped():
    # Bailian run, q107: the agent passed '{"answer": "培养计划课程"}' as the answer text,
    # and the validator accepted the wrapper itself as a string answer.
    from tableqa.schema import normalize_response

    raw = json.dumps({"answer": '\n{"answer": "培养计划课程"}\n', "calculations": []}, ensure_ascii=False)
    assert normalize_response(raw, "extract", "string") == "培养计划课程"
    wrapped = json.dumps(
        {
            "answer": json.dumps(
                {
                    "answer": None,
                    "calculations": [{"index": None, "expression": "1+2", "decimals": None, "suffix": ""}],
                }
            )
        }
    )
    # The calculations compute the whole answer, even when the answer itself is null.
    assert normalize_response(wrapped, "thinking", "number") == "3"
    # A JSON-looking text answer with keys of its own is a real answer, not a wrapper.
    literal = json.dumps({"answer": json.dumps({"answer": "A", "notes": "B"})})
    assert normalize_response(literal, "extract", "string") == '{"answer": "A", "notes": "B"}'
    structure = {
        "row_count": 1,
        "col_count": 1,
        "cells": [{"text": "a", "row": 0, "col": 0, "rowspan": 1, "colspan": 1}],
    }
    raw = json.dumps({"answer": json.dumps(structure)})
    assert '"row_count":1' in normalize_response(raw, "structure", "json")


def test_garbled_pdf_text_layer_is_dropped():
    # Bailian test run, q699: 073.pdf's text layer reads like '!"#$%&' because of broken font maps,
    # yet the prompt calls the text layer more accurate than the image. The agent spent turns on it.
    import pymupdf

    from tableqa.agent import readable

    with pymupdf.open(ROOT / "data/files/073.pdf") as pdf:
        assert not any(readable(page.get_text()) for page in pdf)
    with pymupdf.open(ROOT / "data/files/062.pdf") as pdf:
        assert all(readable(page.get_text()) for page in pdf)
    assert readable("营业收入 1,234.56 万元") and not readable("!\"#$ %&'( )*+,-.")
