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
each stage should teach: `docs/ROADMAP.md`. Current stage: **S1 done → S2 next**.

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
uv run kama run "fix the failing test" # agent run in-process (S1); asks before bash/write_file
uv run kama run -y -w ../other "..."  # auto-approve, different workspace
make live                             # real-API tests (needs ANTHROPIC_API_KEY; costs money)

uv run python -m evals.run_evals list      # eval tasks (docs/EVALS.md explains everything)
make evals-selftest                        # graders vs oracle / null / wrong solutions; free
uv run python -m evals.run_evals run --reps 3 [--variant v1] [--tasks a,b]   # paid
uv run python -m evals.run_evals summary [--variant v1]
```

Agent settings (priority low→high: `~/.kama/.env`, `./.env`, env vars; put the API key in
`~/.kama/.env` so it works from any workspace): `ANTHROPIC_API_KEY`, `KAMA_MODEL` (default `claude-opus-5`),
`KAMA_MAX_STEPS` (30), `KAMA_MAX_TOKENS` (16000), `KAMA_EFFORT` (unset = API default),
`KAMA_REFUSAL_FALLBACK` (true; only sent for models that support it), `KAMA_RUNS_DIR` (`.kama/runs`).

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
    config.py            defaults -> ~/.kama/.env -> ./.env -> env vars (pydantic-validated)
    app.py               CoreApp: wires handlers, signal handling, lifecycle
    llm/types.py         LLMProvider protocol, LLMResponse (raw blocks + parsed views), Usage
    llm/anthropic_provider.py  Messages API via raw SDK; error mapping; caching; fallbacks
    tools/base.py        Tool[Params] ABC, ToolResult, workspace path confinement
    tools/registry.py    validate input -> run -> every failure becomes an is_error result
    tools/builtin.py     read_file, list_dir, write_file, bash
    agent/loop.py        AgentLoop: model -> tools -> results -> repeat; emits run events
    agent/sinks.py       EventSink protocol; events.jsonl writer; console printer
    agent/runner.py      run_goal(): run id, run dir, provider, registry, sinks
  cli/main.py            argparse CLI; maps failures to exit codes
tests/fakes.py           ScriptedProvider: canned LLM responses, records requests
tests/unit/              protocol, config, server, tools, loop, provider (mock HTTP)
tests/integration/       real daemon + CLI subprocesses
tests/live/              real API; deselected by default
evals/harness.py         trial runner: fresh workspace, end-state grading, results/errors/traces
evals/run_evals.py       CLI: list / selftest / run / summary; harness-approval gate
evals/tasks/<id>/        task.toml + fixture/ + [setup.py] + check.py + oracle/ + wrong/*/ + [alt/*/]
                         (log-error-triage is the reference task)
evals/results/kama-run/<variant>/  results.jsonl, errors.jsonl (traces/, events/ git-ignored)
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
- Agent history is append-only; assistant content blocks are echoed back verbatim
  (thinking signatures, fallback blocks). Never edit or re-serialize earlier turns.
- Each `tool_use` gets exactly one `tool_result`, same order, all in one user message.
- Every run writes `run.started` first and `run.finished` last, even on API errors,
  internal bugs and cancellation. `events.jsonl` alone must be enough to reconstruct a run.
- Tool failures (bad input, missing file, denied, crash) go back to the model as
  `is_error` results and never raise out of the registry. A non-zero `bash` exit code
  is a normal result, not an error.
- File tools cannot leave the workspace (symlinks included). Blocking I/O in tools goes
  through `asyncio.to_thread`, because the loop moves into the daemon's event loop in S2.
- Tool specs are sorted and the system prompt holds nothing volatile, which keeps the
  prompt-cache prefix stable.

- Evals grade the end state of a fresh workspace with hidden checks, never the agent's
  own claims. Every task has an `oracle/` that passes and at least one `wrong/` that
  fails (and every `alt/` passes); `selftest` enforces it inside `make verify`. Generated
  inputs (`setup.py`) are seeded, and their hash is pinned in a test.
- Infra failures (API error, timeout, grader crash, wrong served model) go to
  `errors.jsonl` and never count as a score. Only the user approves the harness hash
  (`--approve-harness`); never pass it on their behalf.

### Conventions

- Python 3.12, `mypy --strict`, ruff (line length 100). `make verify` must pass before commit.
- Comments/docstrings in English, short, and only where the *why* isn't obvious.
- Tests use real sockets on port 0 and real subprocesses rather than mocking the transport.
  Wait on a readiness signal, never a fixed sleep.
- Agent-loop tests use `tests/fakes.py::ScriptedProvider`; provider tests mock HTTP with
  `httpx2.MockTransport` (the anthropic 1.x SDK is built on httpx2, not httpx).
- LLM calls (S1+) go through the raw provider SDK behind our own `LLMProvider` interface;
  no agent frameworks in this repo.
- Secrets live in `.env` (git-ignored); never commit keys.
