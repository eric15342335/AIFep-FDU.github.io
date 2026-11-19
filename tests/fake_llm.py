"""Scripted OpenAI-compatible server for an offline end-to-end sandbox test.

Turn 1: call zoom. Turn 2: stop with the answer only in the thinking and no tool call, which ends a
session unless the harness reminds the model. Turn 3 (after that reminder): call answer.
Streams SSE like an OpenAI-compatible server.
Run inside the sandbox image on the internal network with alias `litellm`.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def reply_for(messages):
    images = sum(
        1
        for m in messages
        if isinstance(m.get("content"), list)
        for part in m["content"]
        if part.get("type") == "image_url"
    )
    tool_results = [m for m in messages if m.get("role") == "tool"]
    replayed = [m.get("reasoning_content") for m in messages if m.get("role") == "assistant"]
    print(
        f"request: {len(messages)} messages, {images} images, {len(tool_results)} tool results, "
        f"replayed reasoning: {replayed}",
        flush=True,
    )
    if not tool_results:
        return "zoom", {"page": 1, "x0": 0, "y0": 0, "x1": 500, "y1": 500}
    # Until the harness reminds it, stop without a tool call.
    if "还没有调用 answer" not in json.dumps(messages, ensure_ascii=False):
        return None, {}
    if images < 2:  # first page image + the zoom crop
        return "answer", {"answer": "MISSING_TOOL_IMAGE"}
    return "answer", {"answer": "42"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # pylint: disable=invalid-name  # name required by BaseHTTPRequestHandler
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        name, args = reply_for(body["messages"])
        # Every top-level field except the bulky ones, as the upstream received it after LiteLLM.
        print("params:", {k: v for k, v in body.items() if k not in ("messages", "tools")}, flush=True)
        print("tools:", [t["function"]["name"] for t in body.get("tools", [])], flush=True)
        print("authorization header set:", bool(self.headers.get("Authorization")), flush=True)
        if name is None:
            self._stop()
            return
        call = {
            "index": 0,
            "id": f"call_{name}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "reasoning_content": f"thinking before {name}"},
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    "prompt_tokens_details": {"cached_tokens": 60},
                },
            },
        ]
        if not body.get("stream"):
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            data = {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}]}
            self._send("application/json", json.dumps(data).encode())
            return
        self._stream(chunks)

    def _stop(self):
        """Reply with thinking only and finish_reason "stop", as the model did in the local run's q13."""
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "sum is 42"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}},
        ]
        self._stream(chunks)

    def _stream(self, chunks):
        events = [f"data: {json.dumps({'id': 'x', 'object': 'chat.completion.chunk', **c})}" for c in chunks]
        self._send("text/event-stream", "".join(e + "\n\n" for e in [*events, "data: [DONE]"]).encode())

    def _send(self, content_type, data):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 4000), Handler).serve_forever()
