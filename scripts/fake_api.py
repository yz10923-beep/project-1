"""Fake Anthropic Messages API for offline end-to-end checks (no key, no cost).

Streams SSE with realistic timing (~0.8s to first token, then ~30ms per word): a
tool_use step running `sleep 1; python3 --version`, then a final answer.

    python3 scripts/fake_api.py 7622 &
    ANTHROPIC_BASE_URL=http://127.0.0.1:7622 ANTHROPIC_API_KEY=fake uv run kama-core
    uv run kama run -y "check python" && uv run kama trace
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS = {"n": 0}
REPLIES = [
    ("Let me check the Python version.", {"id": "tu_1", "name": "bash", "input": {"command": "sleep 1; python3 --version"}}, "tool_use"),
    ("Python is installed and working. " * 6, None, "end_turn"),
]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"]))
        text, tool, stop = REPLIES[min(CALLS["n"], len(REPLIES) - 1)]
        CALLS["n"] += 1
        self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
        def ev(d):
            self.wfile.write(f"event: {d['type']}\ndata: {json.dumps(d)}\n\n".encode()); self.wfile.flush()
        ev({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 1400 * CALLS["n"], "cache_creation_input_tokens": 300}}})
        time.sleep(0.8)  # time to first token
        ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        for w in text.split(" "):
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w + " "}}); time.sleep(0.03)
        ev({"type": "content_block_stop", "index": 0})
        if tool:
            ev({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": tool["id"], "name": tool["name"], "input": {}}})
            ev({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool["input"])}})
            ev({"type": "content_block_stop", "index": 1})
        ev({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 40 + 8 * len(text.split())}})
        ev({"type": "message_stop"})

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
