"""Scripted OpenAI-compatible server for an offline end-to-end sandbox test.

Turn 1: call zoom. Turn 2 (after the crop comes back as an image): call answer.
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
    print(f"request: {len(messages)} messages, {images} images, {len(tool_results)} tool results", flush=True)
    if not tool_results:
        return "zoom", {"page": 1, "x0": 0, "y0": 0, "x1": 500, "y1": 500}
    if images < 2:  # first page image + the zoom crop
        return "answer", {"answer": "MISSING_TOOL_IMAGE"}
    return "answer", {"answer": "42"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        name, args = reply_for(body["messages"])
        keys = ("chat_template_kwargs", "temperature", "top_p", "top_k", "max_tokens")
        print("params:", {k: body.get(k) for k in keys}, flush=True)
        call = {
            "index": 0,
            "id": f"call_{name}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}},
        ]
        if not body.get("stream"):
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            data = {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}]}
            self._send("application/json", json.dumps(data).encode())
            return
        stream = (
            "".join(
                f"data: {json.dumps({'id': 'x', 'object': 'chat.completion.chunk', **c})}\n\n" for c in chunks
            )
            + "data: [DONE]\n\n"
        )
        self._send("text/event-stream", stream.encode())

    def _send(self, content_type, data):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 4000), Handler).serve_forever()
