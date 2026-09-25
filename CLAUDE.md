# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Who I am
I'm Yi Zhou, an MSIS student at NYU (graduating December 2026), with an undergraduate
degree from the University of Washington. I'm working toward a role as an LLM application
engineer (LLM应用工程师), ideally building AI agents for finance.

## What I can already do
- Python and SQL (MySQL) are my working languages. I can read Java and C++ and make
  targeted changes with AI assistance, but I'm not proficient in either, and I don't know Go.
- Background across NLP/ML coursework, distributed systems, database systems, and
  quantitative finance.
- Summer capstone with Citi (Robothon Global Markets, an algorithmic trading competition
  platform):
  - Diagnosed a RabbitMQ capacity ceiling as per-client connection cost rather than message
    throughput, disproving the initial hypothesis with a controlled load test. Collapsing
    connections across the Python, Java and C++ bot clients roughly doubled sustainable bot
    count under a fixed connection budget.
  - Built an operator console (Flask, Chart.js) showing live order flow and order-book depth,
    derived by parsing component logs; integrated Elasticsearch, Kibana and Filebeat.
  - Found silent production failures through end-to-end verification, including a logging
    pipeline that dropped 100% of events while every service reported healthy.
- Currently working through a staged project that builds a local coding-agent runtime,
  deployed on my own Linux server.

## Where I'm weak
- Building an agent end to end from a blank repo: architecture, tool design, memory,
  failure handling.
- Retrieval engineering: chunking, hybrid search, reranking, vector stores (e.g. pgvector).
- Evaluation and observability for LLM systems: eval sets, metrics, tracing
  (Langfuse / LangSmith).
- Async Python and production deployment patterns for LLM services.

## What I'm building next
An incident-triage agent over my capstone's ELK logging stack, with a frozen evaluation
harness: a labelled incident set, macro-F1 on classification, evidence grounding, and
trajectory efficiency.

## How I want you to help
- Treat me as a capable engineer who learns fastest by building. Prefer concrete code,
  architecture sketches and trade-offs over general explanations.
- Default to Python. Only suggest another language if a specific role or task requires it.
- Teach fundamentals that won't churn — raw LLM API mechanics, retrieval, evals,
  observability — before frameworks (LangGraph, LlamaIndex).
- Push me toward finished, deployed, measured work. If I start scattering across new
  languages or tools without finishing, say so directly.
- Be honest when my approach is weak or my understanding is wrong. Point out gaps an
  interviewer would notice.
- When relevant, connect advice to finance use cases and to how it would read on a resume
  or in an interview.

---

## This project

A from-scratch reimplementation of [KamaClaude](https://github.com/youngyangyang04/KamaClaude)
(MIT): a mini local coding-agent runtime in the style of Claude Code. A long-running
`kama-core` daemon owns all agent state; `kama` (CLI) and later a TUI are thin clients
talking JSON-RPC 2.0 over NDJSON/TCP.

The reference repo has `stage/s0` … `stage/s7` branches. Use them to compare designs
after building a stage, not as a source to copy. Stage plan, done-criteria and what
each stage should teach: `docs/ROADMAP.md`. Current stage: **S0 done → S1 next**.

### Commands

```bash
uv sync                               # install (Python 3.12)
make verify                           # ruff lint + format check, mypy --strict, pytest
uv run pytest tests/unit -v           # fast, in-process (real sockets, ephemeral ports)
uv run pytest tests/integration -v    # spawns real kama-core / kama processes
uv run pytest tests/unit/test_server.py::test_ping_roundtrip -v

uv run kama-core                      # daemon, foreground; Ctrl+C / SIGTERM to stop
KAMA_PORT=8000 uv run kama-core       # config via KAMA_* env vars or .env
uv run kama ping                      # exit 0 ok, 1 rpc error, 2 bad config, 3 daemon unreachable
```

### Layout

```
src/kama_claude/
  core/
    bus/envelope.py      JSON-RPC 2.0 request/success/error models + error codes
    bus/commands.py      per-method params/result models (the command contract)
    bus/events.py        server-pushed events: discriminated union on `type`
    transport/framing.py NDJSON read/write, 1 MiB frame cap
    transport/server.py  JsonRpcServer: register(method, ParamsModel, handler)
    transport/client.py  JsonRpcClient: call(method, params, ResultModel)
    config.py            defaults -> .env -> KAMA_* env vars (pydantic-validated)
    app.py               CoreApp: wires handlers, signal handling, lifecycle
  cli/main.py            argparse CLI; maps failures to exit codes
tests/unit/              protocol, config, in-process server
tests/integration/       real daemon + CLI subprocesses
```

### Invariants (keep these true)

- Every request line gets exactly one response line; `_dispatch` never raises.
- Error codes follow JSON-RPC 2.0: bad JSON → -32700, bad envelope → -32600,
  unknown method → -32601, params failing validation → -32602, handler crash → -32603.
  Handler exception text is logged, never sent to clients.
- A malformed request must not kill the connection; an oversized frame does (framing is lost).
- The daemon installs signal handlers *before* logging `listening on`, which is the
  readiness signal tests wait for.
- Adding a command = params + result model in `bus/commands.py`, handler registered in
  `CoreApp.__init__`, a client call, and unit + integration tests.

### Conventions

- Python 3.12, `mypy --strict`, ruff (line length 100). `make verify` must pass before commit.
- Comments/docstrings in English, short, and only where the *why* isn't obvious.
- Tests use real sockets on port 0 and real subprocesses rather than mocking the transport.
  Wait on a readiness signal, never a fixed sleep.
- LLM calls (S1+) go through the raw provider SDK behind our own `LLMProvider` interface;
  no agent frameworks in this repo.
- Secrets live in `.env` (git-ignored); never commit keys.
