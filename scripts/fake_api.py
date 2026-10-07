"""Fake Anthropic Messages API for offline end-to-end checks (no key, no cost).

Streams SSE with realistic timing (~0.8s to first token, then ~30ms per word). The
scripted run exercises planning (S3): it writes a plan and starts task 1 in one turn
(parallel tool calls), runs `sleep 1; python3 --version` and saves a note about it (S4),
stops early with task 2 still open, gets the loop's reminder, finishes the plan, answers.
A later run in the same session gets the final answer straight away.

Stateless: the reply is picked by how many assistant turns the request already holds,
so any number of runs can use one server. FAKE_API_FAST=1 drops the delays (tests).

FAKE_API_FAULTS (S5) makes the first requests fail, one fault per request, in order:
529 (overloaded), 500, 400 (permanent), 429@SECONDS (rate limited, with retry-after),
stream (text starts streaming, then an overloaded error event mid-response).
FAKE_API_MAX_PROMPT_CHARS (S6) answers 400 "prompt is too long" past that many chars.
FAKE_API_TOKENS_PER_CHAR (S6) reports request sizes as 1000 + that x the messages' chars
(and answers /v1/messages/count_tokens the same), so a small KAMA_CONTEXT_BUDGET is
crossed. On-demand compaction is emulated: a request with `compaction` gets one block
back (the beta header is required, as on the real API); a request that carries a block
must have it first and send the header, or it gets the API's 400.
    FAKE_API_FAULTS=529,stream,429@0.2 python3 scripts/fake_api.py 7622

    python3 scripts/fake_api.py 7622 &
    ANTHROPIC_BASE_URL=http://127.0.0.1:7622 ANTHROPIC_API_KEY=fake uv run kama-core
    uv run kama run -y "check python" && uv run kama trace
"""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = 0.0 if os.environ.get("FAKE_API_FAST") else 1.0
FAULTS = [f for f in os.environ.get("FAKE_API_FAULTS", "").split(",") if f]
# S6: a request whose messages serialize to more than this many chars is "too long", as
# the real API answers a prompt over the model's window (0 = no limit).
MAX_PROMPT_CHARS = int(os.environ.get("FAKE_API_MAX_PROMPT_CHARS", "0"))
TOKENS_PER_CHAR = float(os.environ.get("FAKE_API_TOKENS_PER_CHAR", "0"))
COMPACT_BETA = "compact-2026-09-04"

def tokens_of(body):
    return 1000 + int(len(json.dumps(body["messages"])) * TOKENS_PER_CHAR)

def turns_done(messages):
    """Assistant turns so far. A compaction block (always first) stands for the turns it
    summarized: its signature carries their count, so the script picks up after them."""
    n = 0
    for i, m in enumerate(messages):
        c = m["content"]
        if i == 0 and isinstance(c, list) and c and c[0].get("type") == "compaction":
            n += int(c[0]["signature"].split(":")[1])
        elif m["role"] == "assistant":
            n += 1
    return n

def compaction_problem(body, beta):
    for i, m in enumerate(body["messages"]):
        c = m["content"]
        blocks = [b for b in c if b.get("type") == "compaction"] if isinstance(c, list) else []
        if blocks and (i != 0 or c[0].get("type") != "compaction"):
            return "compaction_block_misplaced"
        if blocks and COMPACT_BETA not in beta:
            return "messages.0.content.0: 'compaction' is not one of the expected content block types"
    if "compaction" in body and COMPACT_BETA not in beta:
        return f"the compaction parameter requires anthropic-beta: {COMPACT_BETA}"
    return None
_lock = threading.Lock()

def next_fault():
    with _lock:
        return FAULTS.pop(0) if FAULTS else None

ERROR_TYPES = {529: "overloaded_error", 500: "api_error", 400: "invalid_request_error", 429: "rate_limit_error"}

def tool(i, name, **inp):
    return {"id": f"tu_{i}", "name": name, "input": inp}

REPLIES = [
    ("I'll plan this first.", [tool(1, "task_create", tasks=[{"title": "Check the Python version"},
                                                             {"title": "Report it", "blocked_by": [1]}]),
                               tool(2, "task_update", updates=[{"id": 1, "status": "in_progress"}])], "tool_use"),
    ("Checking.", [tool(3, "bash", command="sleep 1; python3 --version"),
                   tool(6, "note_save", text="python3 is on PATH here", source="python3 --version")],
     "tool_use"),
    ("", [tool(4, "task_update", updates=[{"id": 1, "status": "completed"}])], "tool_use"),
    ("Python 3 is installed.", [], "end_turn"),  # task 2 still open: expect a reminder
    ("", [tool(5, "task_update", updates=[{"id": 2, "status": "completed"}])], "tool_use"),
    ("Python is installed and working. " * 6, [], "end_turn"),
]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        beta = self.headers.get("anthropic-beta", "")
        if (problem := compaction_problem(body, beta)) is not None:
            return self.send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": problem}})
        if self.path.startswith("/v1/messages/count_tokens"):
            return self.send_json(200, {"input_tokens": tokens_of(body) if TOKENS_PER_CHAR else 1500})
        if "compaction" in body:
            n = turns_done(body["messages"])
            block = {"type": "compaction", "content": f"Summary of {n} assistant turns: python3 is installed (fake).", "signature": f"fake:{n}"}
            return self.send_json(200, {
                "id": "m", "type": "message", "role": "assistant", "model": body["model"], "content": [block],
                "stop_reason": "compaction", "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "iterations": [{"type": "compaction", "input_tokens": tokens_of(body), "output_tokens": 60}]}})
        n = turns_done(body["messages"])
        text, tools, stop = REPLIES[min(n, len(REPLIES) - 1)]
        fault = next_fault()
        size = len(json.dumps(body["messages"]))
        if MAX_PROMPT_CHARS and size > MAX_PROMPT_CHARS:
            msg = f"prompt is too long: {size // 4} tokens > {MAX_PROMPT_CHARS // 4} maximum"
            err = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": msg}}).encode()
            self.send_response(400); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(err))); self.end_headers()
            self.wfile.write(err)
            return
        if fault is not None and fault != "stream":
            code, _, wait = fault.partition("@")
            err = json.dumps({"type": "error", "error": {"type": ERROR_TYPES[int(code)], "message": f"fake {code}"}}).encode()
            self.send_response(int(code)); self.send_header("content-type", "application/json")
            if wait:
                self.send_header("retry-after-ms", str(int(float(wait) * 1000)))
            self.send_header("content-length", str(len(err))); self.end_headers()
            self.wfile.write(err)
            return
        self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
        def ev(d):
            self.wfile.write(f"event: {d['type']}\ndata: {json.dumps(d)}\n\n".encode()); self.wfile.flush()
        if fault == "stream":
            text = "this text is cut off and must be discarded"
        ev({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1,
                      "cache_read_input_tokens": tokens_of(body) - 312 if TOKENS_PER_CHAR else 1400 * (n + 1),
                      "cache_creation_input_tokens": 300}}})
        time.sleep(0.8 * DELAY)  # time to first token
        index = 0
        if text:
            ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            for w in text.split(" "):
                ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w + " "}}); time.sleep(0.03 * DELAY)
            if fault == "stream":
                ev({"type": "error", "error": {"type": "overloaded_error", "message": "fake mid-stream overload"}})
                return
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
