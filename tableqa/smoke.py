"""Endpoint smoke test: everything the harness relies on, checked with real requests."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw

ZOOM_TOOL = {
    "type": "function",
    "function": {
        "name": "zoom",
        "description": "Crop and enlarge a region of the page. Coordinates 0-1000.",
        "parameters": {
            "type": "object",
            "required": ["page", "x0", "y0", "x1", "y1"],
            "properties": {
                "page": {"type": "integer"},
                "x0": {"type": "number"},
                "y0": {"type": "number"},
                "x1": {"type": "number"},
                "y1": {"type": "number"},
            },
        },
    },
}


def post(base_url: str, path: str, payload: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    request = Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read(600).decode('utf-8', 'replace')}") from exc


def image_part(image: Image.Image) -> dict[str, Any]:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()},
    }


def number_card(text: str, size: tuple[int, int] = (640, 240)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((40, 80), text, fill="black", font_size=96)
    return image


def run(base_url: str, model: str, page: Path) -> list[tuple[str, bool, str]]:
    off = {"chat_template_kwargs": {"enable_thinking": False}}

    def chat(messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        payload = {"model": model, "messages": messages, "max_tokens": 2048, "temperature": 0, **off, **extra}
        return post(base_url, "/chat/completions", payload)["choices"][0]["message"]

    def text_check() -> str:
        reply = chat([{"role": "user", "content": "Reply with exactly: OK"}])
        assert "OK" in (reply.get("content") or ""), reply
        return reply["content"].strip()

    def image_check() -> str:
        reply = chat(
            [
                {
                    "role": "user",
                    "content": [
                        image_part(number_card("47391")),
                        {"type": "text", "text": "What number is shown? Digits only."},
                    ],
                }
            ]
        )
        assert "47391" in (reply.get("content") or ""), reply
        return reply["content"].strip()

    def baseline_image_check() -> str:
        # The baseline adds a per-image max_pixels key (DashScope extension).
        part = image_part(number_card("8642")) | {"max_pixels": 5760000}
        reply = chat(
            [{"role": "user", "content": [part, {"type": "text", "text": "Number? Digits only."}]}],
            response_format={"type": "json_object"},
        )
        return (reply.get("content") or "").strip()

    def tool_call_check() -> str:
        page_image = Image.open(page).convert("RGB")
        reply = chat(
            [
                {
                    "role": "user",
                    "content": [
                        image_part(page_image),
                        {"type": "text", "text": "Use the zoom tool on the top-left quarter of page 1."},
                    ],
                }
            ],
            tools=[ZOOM_TOOL],
            tool_choice="auto",
        )
        calls = reply.get("tool_calls") or []
        assert calls and calls[0]["function"]["name"] == "zoom", reply
        json.loads(calls[0]["function"]["arguments"])
        return calls[0]["function"]["arguments"]

    def tool_result_image_check() -> str:
        # Same message shape pi uses for openai-completions: tool text, then a user message with the image.
        call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "zoom", "arguments": '{"page":1,"x0":0,"y0":0,"x1":500,"y1":500}'},
        }
        reply = chat(
            [
                {"role": "user", "content": "Zoom into page 1 and tell me the number you see. Digits only."},
                {"role": "assistant", "content": "", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "call_1", "content": "page 1, region (0, 0, 500, 500)"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Attached image(s) from tool result:"},
                        image_part(number_card("90517")),
                    ],
                },
            ],
            tools=[ZOOM_TOOL],
        )
        assert "90517" in (reply.get("content") or ""), reply
        return reply["content"].strip()

    def many_images_check() -> str:
        parts = [image_part(number_card(str(i), (200, 120))) for i in range(30)]
        reply = chat(
            [{"role": "user", "content": [*parts, {"type": "text", "text": "How many images? Number only."}]}]
        )
        return (reply.get("content") or "").strip()

    def thinking_check() -> str:
        reply = chat(
            [{"role": "user", "content": "What is 17*23? Number only."}],
            chat_template_kwargs={"enable_thinking": True, "reasoning_effort": "xhigh"},
        )
        reasoning = reply.get("reasoning_content") or reply.get("reasoning") or ""
        assert reasoning and "<think>" not in (reply.get("content") or ""), reply
        return f"reasoning {len(reasoning)} chars, answer {reply.get('content', '').strip()}"

    def responses_api_check() -> str:
        result = post(
            base_url,
            "/responses",
            {"model": model, "input": "Reply with exactly: OK", "max_output_tokens": 256},
        )
        return str(result.get("output_text") or result.get("status"))

    checks: list[tuple[str, Callable[[], str]]] = [
        ("text", text_check),
        ("image input", image_check),
        ("baseline image + json mode", baseline_image_check),
        ("tool call", tool_call_check),
        ("image in tool result (pi format)", tool_result_image_check),
        ("30 images in one request", many_images_check),
        ("thinking on, parsed", thinking_check),
        ("responses API (info only)", responses_api_check),
    ]
    results = []
    for name, check in checks:
        try:
            results.append((name, True, check()[:120]))
        except (AssertionError, RuntimeError, URLError, TimeoutError, KeyError, ValueError) as exc:
            results.append((name, False, str(exc)[:300]))
    return results
