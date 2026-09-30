"""Fake Anthropic Messages API for offline end-to-end checks (no key, no cost).

Streams SSE with realistic timing (~0.8s to first token, then ~30ms per word). The
scripted run exercises planning (S3): it writes a plan and starts task 1 in one turn
(parallel tool calls), runs `sleep 1; python3 --version`, stops early with task 2
still open, gets the loop's reminder, finishes the plan, then answers.

Stateless: the reply is picked by how many assistant turns the request already holds,
so any number of runs can use one server. FAKE_API_FAST=1 drops the delays (tests).

    python3 scripts/fake_api.py 7622 &
    ANTHROPIC_BASE_URL=http://127.0.0.1:7622 ANTHROPIC_API_KEY=fake uv run kama-core
    uv run kama run -y "check python" && uv run kama trace
"""
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = 0.0 if os.environ.get("FAKE_API_FAST") else 1.0

def tool(i, name, **inp):
    return {"id": f"tu_{i}", "name": name, "input": inp}

REPLIES = [
    ("I'll plan this first.", [tool(1, "task_create", tasks=["Check the Python version", "Report it"]),
                               tool(2, "task_update", id=1, status="in_progress")], "tool_use"),
    ("Checking.", [tool(3, "bash", command="sleep 1; python3 --version")], "tool_use"),
    ("", [tool(4, "task_update", id=1, status="completed")], "tool_use"),
    ("Python 3 is installed.", [], "end_turn"),  # task 2 still open: expect a reminder
    ("", [tool(5, "task_update", id=2, status="completed")], "tool_use"),
    ("Python is installed and working. " * 6, [], "end_turn"),
]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        n = sum(m["role"] == "assistant" for m in body["messages"])
        text, tools, stop = REPLIES[min(n, len(REPLIES) - 1)]
        self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
        def ev(d):
            self.wfile.write(f"event: {d['type']}\ndata: {json.dumps(d)}\n\n".encode()); self.wfile.flush()
        ev({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 1400 * (n + 1), "cache_creation_input_tokens": 300}}})
        time.sleep(0.8 * DELAY)  # time to first token
        index = 0
        if text:
            ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            for w in text.split(" "):
                ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w + " "}}); time.sleep(0.03 * DELAY)
            ev({"type": "content_block_stop", "index": 0})
            index = 1
        for t in tools:
            ev({"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": t["id"], "name": t["name"], "input": {}}})
            ev({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(t["input"])}})
            ev({"type": "content_block_stop", "index": index})
            index += 1
        ev({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 40 + 8 * len(text.split())}})
        ev({"type": "message_stop"})

server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H)
print("fake api listening", flush=True)  # readiness line for tests
server.serve_forever()
