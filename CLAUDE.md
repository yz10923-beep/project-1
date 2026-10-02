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
each stage should teach: `docs/ROADMAP.md`. Current stage: **S4 done (full version; memory A/B pending) → S5 next**.
No stage is timeboxed or cut: build the fullest version of each.

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
uv run kama run "fix the failing test" # runs in kama-core; streams; asks before bash/write_file
uv run kama run -y -w ../other "..."  # auto-approve, different workspace
uv run kama run --detach "..."        # print run id and return; the run keeps going
uv run kama attach RUN_ID             # watch (and answer approvals) from another terminal
uv run kama runs | kama cancel RUN_ID
uv run kama plan show|add|cancel RUN_ID ...   # read a plan; steer a live one
uv run kama tui [RUN_ID] [-w DIR] [-y] [--session ID]   # full-screen UI (also: kama-tui)
uv run kama chat [-w DIR] [-y] [--session ID]    # a conversation: each line a run in one session
uv run kama run --new-session|--session ID "..."  # runs that share a conversation
uv run kama session list|show ID · kama notes list|add|edit|rm   # sessions; the agent's memory
uv run kama run --local "..."         # in-process, no daemon (S1 behaviour)
uv run kama trace [RUN_ID]            # where a run's time/tokens/cost went (default: latest)
uv run kama trace RUN_ID --chrome t.json   # open in https://ui.perfetto.dev
python3 scripts/fake_api.py 7622 &    # offline end-to-end: ANTHROPIC_BASE_URL=http://127.0.0.1:7622
make live                             # real-API tests (needs ANTHROPIC_API_KEY; costs money)

uv run python -m evals.run_evals list      # eval tasks (docs/EVALS.md explains everything)
make evals-selftest                        # graders vs oracle / null / wrong solutions; free
uv run python -m evals.run_evals run --reps 3 [--variant v1] [--tasks a,b]   # paid
uv run python -m evals.run_evals summary [--variant v1]
```

Agent settings (priority low→high: `~/.kama/.env`, `./.env`, env vars; put the API key in
`~/.kama/.env` so it works from any workspace): `ANTHROPIC_API_KEY`, `KAMA_MODEL` (default `claude-opus-5`),
`KAMA_MAX_STEPS` (30), `KAMA_MAX_TOKENS` (16000), `KAMA_EFFORT` (unset = API default),
`KAMA_REFUSAL_FALLBACK` (true; only sent for models that support it), `KAMA_PLANNING` (true;
false = no task_* tools, the S2 agent, for A/B runs), `KAMA_MEMORY` (true; false = no session
history and no notes, the S3 agent), `KAMA_SESSIONS_DIR` (`~/.kama/sessions`), `KAMA_MEMORY_DIR`
(`~/.kama/memory`), `KAMA_RUNS_DIR` (`~/.kama/runs`),
`KAMA_TOKEN_FILE` (`~/.kama/core.token`), `KAMA_APPROVAL_TIMEOUT_S` (600).

### Layout

```
src/kama_claude/
  core/
    bus/envelope.py      JSON-RPC 2.0 request/success/error models + error codes
    bus/commands.py      per-method params/result models + notification names (the contract)
    bus/events.py        run events: discriminated union on `type`; durable vs ephemeral
    transport/framing.py NDJSON read/write, 1 MiB frame cap
    transport/server.py  JsonRpcServer + Connection (locked writes, notify, spawn); token auth
    transport/client.py  JsonRpcClient: reader task, concurrent call(), notifications()
    config.py            defaults -> ~/.kama/.env -> ./.env -> env vars (pydantic-validated)
    app.py               CoreApp: token, run.* / approval.respond handlers, lifecycle
    llm/types.py         LLMProvider protocol, LLMResponse (raw blocks + parsed views), Usage
    llm/anthropic_provider.py  Messages API via raw SDK (streamed); TTFT; errors; caching
    llm/pricing.py       one price table; cost_usd(model, usage) (None if unpriced)
    plan.py              Plan (a run's task DAG: add/update/render), PlanTask, statuses
    session.py           SessionStore: a session = its runs in order; history replayed from events
    notes.py             NoteStore (workspace/session scope), NoteBook, memory_preamble()
    trace/span.py        Span: trace_id, span_id, parent_id, kind (agent|llm|tool|bus|ipc)
    trace/tracer.py      Tracer: span() context manager (ContextVar parents), record()
    trace/analyze.py     summarize() (pure), render() text report, to_chrome() export
    tools/base.py        Tool[Params] ABC, ToolResult, workspace path confinement
    tools/registry.py    validate input -> run -> every failure becomes an is_error result
    tools/builtin.py     read_file, list_dir, write_file, bash
    tools/plan_tools.py  task_create, task_update (batched), task_get, task_list (no approval)
    tools/note_tools.py  note_save, note_update, note_delete, note_list (no approval)
    agent/loop.py        AgentLoop: model -> tools -> results -> repeat; emits run events
    agent/history.py     replay() events -> messages; repair_orphans(); conversation_problems()
    agent/sinks.py       EventSink protocol; events.jsonl writer; console printer
    agent/runner.py      build_loop(), prepare_run() (history + memory block), run_goal()
    agent/manager.py     RunManager (daemon): runs, fan-out with replay, approvals, cancel
  cli/main.py            run / attach / runs / cancel / plan / chat / session / notes / ping /
                         trace / tui; watch()
  tui/state.py           RunView: pure fold of a run's events (dedupe, cost, plan, approvals)
  tui/app.py             Textual app: log, plan panel, approvals, steering, runs, trace, reconnect
scripts/fake_api.py      fake streaming Messages API with realistic timing (offline smoke tests)
tests/fakes.py           ScriptedProvider (streams its text), GatedProvider, PausingProvider
tests/unit/              protocol, config, server, tools, loop, plan, provider (mock SSE), daemon,
                         CLI, TUI (Textual Pilot against an in-process kama-core)
tests/integration/       real daemon + CLI subprocesses (+ the TUI and sessions via the fake API)
tests/live/              real API; deselected by default
evals/harness.py         trial runner: fresh workspace, end-state grading, results/errors/traces
evals/run_evals.py       CLI: list / selftest / run / summary; harness-approval gate
evals/tasks/<id>/        task.toml + fixture/ + [setup.py] + check.py + oracle/ + wrong/*/ + [alt/*/]
                         (log-error-triage is the reference task; `_delete.txt` in a solution deletes;
                         risk-report-spec grades 10 requirements separately, wrong/ from make_answers.py;
                         multi-run tasks: [[runs]] + setup.py between() + _runs.json in solutions)
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
- Every request needs `core.hello` with the daemon's token first (file mode 0600,
  written only after the port is bound). The daemon runs shell commands; 127.0.0.1 is
  not a security boundary.
- A run never waits for a client and outlives every client. Subscribers have bounded
  queues; a slow one gets `run.stream_end` reason `lagged` with `next_seq` and resumes.
  Only `run.cancel` or daemon shutdown stops a run (and it still writes run.finished).
- Subscribe = replay durable events from `from_seq`, then live, with no await between
  the backlog snapshot and registration (no gap, no duplicate). `llm.delta` is
  ephemeral: broadcast live, never persisted or replayed; it has no seq.
- Approval requests and their resolution (by user / auto / timeout) are durable events;
  the first client to answer wins, and unanswered requests are denied after the timeout.
- Run logs live outside workspaces (`~/.kama/runs`), so the agent can't read them.
- Background tasks must log their exceptions (`Connection.spawn` does); a silent task
  crash turns a bug into a hang. pytest has a 60s per-test timeout for the same reason.
- Every run writes `trace.jsonl` next to `events.jsonl`: spans run > step > llm.call /
  tool X > tool.approval / tool.exec, plus `bus.subscribe` (one per client, with delivery
  lag) and `rpc <method>` (IPC). Requests not tied to a run go to `<runs>/_daemon/`.
- Parent links come from a ContextVar, never a global: concurrent tasks must not nest
  into each other. A span from another trace becomes a `linked_span` attr, not a parent.
- Span start is wall-clock (timeline placement); duration is monotonic (NTP-safe).
- Unknown prices are None ("cost unknown"), never $0. Secrets (the token) never reach
  a trace.
- The daemon shares one SDK client per API key (built at startup): constructing one
  costs ~50-80ms and a shared client keeps its connection pool.
- Each run has its own Plan (a task DAG: `blocked_by`, no cycles), changed only by task_*
  tools or a user's `plan.edit`. Every change is atomic and emits `plan.updated` with the
  whole task list (snapshot, not diff) and `by` model|user. Only plan tools may emit a
  model-attributed change. Cancelling needs a note; tasks are never deleted; a task can't
  start or complete while a blocker is open (cancelled blockers count as resolved).
- User plan edits reach the model by appending a text block to the next *unsent* user
  message (never an earlier turn), recorded as `plan.notice`; after pause_turn they wait.
- Ending the turn with open tasks gets one reminder (`plan.reminder`, durable, appended
  as a user message), never more. With planning off the prompt and tools are exactly
  the S2 ones, so A/B runs change one thing.
- Plan-only steps (every tool call a plan tool) don't count against `max_steps`, up to
  `max_steps // 2`; `run.finished` records `plan_only_steps` and `budget_credit`.
- Each stretch of a task in_progress is a `plan` span (monotonic duration) under `run`.
- The TUI is a thin client: everything over the protocol, state folded in `RunView`, and
  after a reconnect it resumes from `next_seq` (nothing shown twice or missed).
- A session stores no messages: its history is `replay`ed from its runs' events.jsonl, and
  the loop builds messages with the same helpers. A continuing run repairs unanswered
  tool_use blocks (is_error results) and records `repaired` in run.started; a trailing user
  turn is joined, never doubled. At most one run per session at a time.
- Notes live outside the workspace, carry source/author/age/volatile, and reach the model
  only as a `<memory>` block before the goal in the run's first user message (never the
  system prompt), framed as past observations, not instructions. No notes and no history
  = no block (request unchanged). KAMA_MEMORY=false = the S3 agent, byte for byte.
- Tests never touch the real home directory: an autouse fixture sets HOME per test.

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
