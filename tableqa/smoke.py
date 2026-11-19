"""Endpoint smoke test: everything the harness relies on, checked with real requests."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, cast

import openai
from openai.types.chat import ChatCompletionMessageParam
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


class Endpoint:
    """Sends small test requests to the model endpoint; each `*_check` method tests one capability."""

    # Bailian's OpenAI-compatible fields, top level (the same ones pi sends; see agent/pi/models.json).
    THINKING_OFF: ClassVar[dict[str, Any]] = {"enable_thinking": False}
    # Fields the OpenAI SDK knows; every other field (the Bailian ones) goes into the request body as is.
    STANDARD: ClassVar[set[str]] = {
        "max_tokens",
        "temperature",
        "tools",
        "tool_choice",
        "response_format",
        "reasoning_effort",
    }

    def __init__(self, base_url: str, model: str, page: Path) -> None:
        self.model, self.page = model, page
        # The gateway needs no key (see agent/litellm.yaml); the SDK insists on one, so pass a placeholder.
        # No retries: a check that only passes on the second try should show up as a failure.
        self.client = openai.OpenAI(base_url=base_url, api_key="none", timeout=300, max_retries=0)

    def call(self, messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        fields = {"max_tokens": 2048, "temperature": 0, **self.THINKING_OFF, **extra}
        known = {k: v for k, v in fields.items() if k in self.STANDARD}
        others = {k: v for k, v in fields.items() if k not in self.STANDARD}
        reply = self.client.chat.completions.with_raw_response.create(
            model=self.model,
            # The checks send DashScope extensions (a per-image max_pixels) that the SDK's types do not list.
            messages=cast(list[ChatCompletionMessageParam], messages),
            extra_body=others,
            **known,
        )
        # The reply as the server sent it, including fields the SDK does not model (reasoning_content).
        # A reply that is not JSON raises ValueError, which marks the check as failed.
        return reply.http_response.json()

    def chat(self, messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return self.call(messages, **extra)["choices"][0]["message"]

    def text_check(self) -> str:
        reply = self.chat([{"role": "user", "content": "Reply with exactly: OK"}])
        assert "OK" in (reply.get("content") or ""), reply
        return reply["content"].strip()

    def image_check(self) -> str:
        reply = self.chat(
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

    def baseline_image_check(self) -> str:
        # The baseline adds a per-image max_pixels key (DashScope extension).
        part = image_part(number_card("8642")) | {"max_pixels": 5760000}
        reply = self.chat(
            [
                {
                    "role": "user",
                    "content": [part, {"type": "text", "text": 'Number? Reply as JSON {"n": "<digits>"}.'}],
                }
            ],
            response_format={"type": "json_object"},
        )
        return (reply.get("content") or "").strip()

    def tool_call_check(self) -> str:
        page_image = Image.open(self.page).convert("RGB")
        reply = self.chat(
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

    def tool_result_image_check(self) -> str:
        # Same message shape pi uses for openai-completions: tool text, then a user message with the image.
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "zoom", "arguments": '{"page":1,"x0":0,"y0":0,"x1":500,"y1":500}'},
        }
        reply = self.chat(
            [
                {"role": "user", "content": "Zoom into page 1 and tell me the number you see. Digits only."},
                {"role": "assistant", "content": "", "tool_calls": [tool_call]},
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

    def many_images_check(self) -> str:
        parts = [image_part(number_card(str(i), (200, 120))) for i in range(30)]
        reply = self.chat(
            [{"role": "user", "content": [*parts, {"type": "text", "text": "How many images? Number only."}]}]
        )
        return (reply.get("content") or "").strip()

    def thinking_check(self) -> str:
        result = self.call(
            [{"role": "user", "content": "What is 17*23? Number only."}],
            enable_thinking=True,
            reasoning_effort="xhigh",
            temperature=1.0,
        )
        reply, usage = result["choices"][0]["message"], result.get("usage", {})
        reasoning = reply.get("reasoning_content") or reply.get("reasoning") or ""
        assert reasoning and "<think>" not in (reply.get("content") or ""), reply
        details = usage.get("completion_tokens_details") or {}
        return (
            f"reasoning {len(reasoning)} chars ({details.get('reasoning_tokens')} tokens), "
            f"answer {reply.get('content', '').strip()}"
        )

    def high_res_check(self) -> str:
        # A page over the default 2.6 MP cap: the flag must raise its image tokens, not cap them.
        page_image = Image.new("RGB", (1600, 2400), "white")
        message = [{"role": "user", "content": [image_part(page_image), {"type": "text", "text": "OK?"}]}]
        default = self.call(message, max_tokens=8)["usage"]["prompt_tokens"]
        high = self.call(message, max_tokens=8, vl_high_resolution_images=True)["usage"]["prompt_tokens"]
        assert high > default, (default, high)
        return f"prompt tokens {default} default, {high} with vl_high_resolution_images"

    def preserve_thinking_check(self) -> str:
        # The reasoning pi replays in history must reach the model (and is billed as input).
        history = [
            {"role": "user", "content": "Pick a number."},
            {"role": "assistant", "content": "7", "reasoning_content": "Let me think. " * 200},
            {"role": "user", "content": "Say it again. Number only."},
        ]
        thinking = {"enable_thinking": True, "temperature": 1.0, "max_tokens": 64}
        kept = self.call(history, preserve_thinking=True, **thinking)["usage"]["prompt_tokens"]
        dropped = self.call(history, preserve_thinking=False, **thinking)["usage"]["prompt_tokens"]
        assert kept > dropped + 300, (kept, dropped)
        return f"prompt tokens {kept} with history reasoning, {dropped} without"

    def cache_check(self) -> str:
        # Implicit prefix cache: a repeated long prompt should report cached tokens (not guaranteed).
        rows = "\n".join(f"| row {i} | {i * 37 % 101} | {i * 53 % 97} |" for i in range(400))
        message = [{"role": "user", "content": f"Table:\n{rows}\nReply with exactly: OK"}]
        first = self.call(message, max_tokens=8)["usage"]
        second = self.call(message, max_tokens=8)["usage"]
        cached = (second.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        return f"prompt tokens {first['prompt_tokens']}, cached on repeat {cached}"

    def run_all(self) -> list[tuple[str, bool, str]]:
        checks: list[tuple[str, Callable[[], str]]] = [
            ("text", self.text_check),
            ("image input", self.image_check),
            ("baseline image + json mode", self.baseline_image_check),
            ("tool call", self.tool_call_check),
            ("image in tool result (pi format)", self.tool_result_image_check),
            ("30 images in one request", self.many_images_check),
            ("thinking on, parsed", self.thinking_check),
            ("high-resolution images", self.high_res_check),
            ("preserve_thinking replays reasoning", self.preserve_thinking_check),
            ("implicit prefix cache (info only)", self.cache_check),
        ]
        results = []
        for name, check in checks:
            try:
                results.append((name, True, check()[:120]))
            except (AssertionError, openai.OpenAIError, KeyError, TypeError, ValueError) as exc:
                results.append((name, False, str(exc)[:300]))
        return results


def run(base_url: str, model: str, page: Path) -> list[tuple[str, bool, str]]:
    return Endpoint(base_url, model, page).run_all()
