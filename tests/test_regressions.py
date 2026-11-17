"""Regression tests: one per failure seen in the pilot and full runs (2026-11-17)."""

import importlib.util
import json
import re
from pathlib import Path

import pytest
from PIL import Image

from tableqa.agent import NO_TEXT_NOTE, prepare_materials, task_prompt
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
    # Pilot over a slow tunnel: pi's default 300 s idle timeout aborted uploads the gateway
    # (900 s) was still serving, and each retry re-sent megabytes of images.
    settings = json.loads((ROOT / "agent/pi/settings.json").read_text(encoding="utf-8"))
    gateway = (ROOT / "agent/litellm.yaml").read_text(encoding="utf-8")
    timeout = re.search(r"request_timeout:\s*(\d+)", gateway)
    assert timeout and settings["httpIdleTimeoutMs"] >= int(timeout.group(1)) * 1000


def test_phoenix_message_bodies_stay_off():
    # Full run at 32 workers: with bodies on, Phoenix pinned a CPU and LiteLLM dropped span
    # batches over the 64 MB OTLP limit.
    gateway = (ROOT / "agent/litellm.yaml").read_text(encoding="utf-8")
    assert re.search(r"^\s*turn_off_message_logging:\s*true\s*$", gateway, re.M)
