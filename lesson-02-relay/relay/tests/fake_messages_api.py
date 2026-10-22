"""A local stand-in for the Anthropic Messages API, so the live code path runs without a key or network.

It checks the headers a real call needs, then plays a tiny model: first it asks for docs_search,
then it quotes the first doc that came back. Set `fail_with` to make it return an HTTP error.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = "test-key"


class FakeMessagesAPI:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fail_with: int | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _reply(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body, "path": self.path})
                if outer.fail_with:
                    return self._reply(outer.fail_with, {"type": "error", "error": {"message": "injected"}})
                if self.path != "/v1/messages" or self.headers.get("x-api-key") != API_KEY \
                        or self.headers.get("anthropic-version") != "2023-06-01":
                    return self._reply(401, {"type": "error", "error": {"message": "bad auth"}})
                self._reply(200, outer.respond(body))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def respond(self, body: dict) -> dict:
        n = len(self.requests)
        tokens_in = len(json.dumps(body)) // 4
        last = body["messages"][-1]["content"]
        results = [b for b in last if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            docs = json.loads(results[0]["content"])
            top = docs[0]
            reply = f"{top['title']}: {top['body'].split('. ')[1].rstrip('.')}."
            return {"content": [{"type": "text", "text": json.dumps({"decision": "answer", "reply": reply})}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": tokens_in, "output_tokens": 30}}
        ticket = body["messages"][0]["content"][0]["text"]
        ask = re.search(r"How do I (.+?) for (\w+)\?", ticket)
        return {"content": [{"type": "text", "text": "Checking the docs."},
                            {"type": "tool_use", "id": f"toolu_fake{n:03d}", "name": "docs_search",
                             "input": {"query": f"{ask.group(1)} {ask.group(2)}"}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": tokens_in, "output_tokens": 25, "cache_read_input_tokens": 0}}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
