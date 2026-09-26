# Roadmap

Stages mirror the reference project's `stage/s*` branches. Each stage ends with
something runnable and a test that proves it. **Done** means `make verify` passes and
the demo command works, not "the code is written".

| Stage | Build | Done when | Fundamental it teaches |
|---|---|---|---|
| **S0** ✅ | CLI ↔ daemon over JSON-RPC 2.0 / NDJSON / TCP; typed protocol; config | `kama ping` returns pong from a separately running `kama-core`; error codes tested | Process boundaries, wire contracts, asyncio streams |
| **S1** ✅ | `kama run "<goal>"`: agent loop (LLM → tool_use → tool_result → …) with read_file / list_dir / write_file / bash; every step appended to `runs/<id>/events.jsonl` | A real goal completes end to end; loop unit-tested against a scripted fake LLM | Raw Messages API mechanics: tool schemas, stop reasons, message assembly |
| **S2** ✅ | Move the runner into the daemon; clients subscribe to an event stream over IPC | Two clients watch the same run live; client crash doesn't kill the run | Pub/sub, backpressure, cancellation in asyncio |
| **Trace** | Span-level trace of IPC → event bus → LLM calls (latency, tokens, cost) | You can replay a run and say where the time and tokens went | Observability: the same idea as Langfuse/LangSmith, built by hand first |
| **S3** | Task tools (create/update/list) so the model plans; TUI | A multi-step goal shows a visible plan being executed | Planning as tools, not prompts |
| **S4** | Sessions: multiple runs share a thread; notes as durable memory | Run 2 uses a fact learned in run 1 without re-reading it | Memory tiers: working context vs. durable notes |
| **S5** | Tool safety: param validation, permission policy + approval flow, failure classification, retry | A denied `bash rm` is blocked and the model recovers; transient errors retry, permanent don't | Failure handling for agents |
| **S6** | Context governance: token budget, tool_result truncation, compaction | A long session stays under budget with measured quality loss | Context engineering, token accounting |
| **S7** | Skills, subagents, MCP client | An MCP server's tools appear in the registry and get called | Extension boundaries |

## Priorities given the actual goal

This project exists to feed the **incident-triage agent over ELK** and a finance-agent
job search. That changes the priority order:

- **Must finish:** S1, Trace, S5, S6. Those are the agent-loop, observability, failure-handling
  and context skills that the triage agent reuses directly and that interviewers ask about.
- **Do properly but quickly:** S2, S4.
- **Timebox hard:** the S3 TUI and S7. A pretty terminal UI is not what gets you hired for
  LLM application work. If the TUI takes more than two days, cut it.
- **Add what the reference lacks:** an eval harness. From S1 on, keep a small frozen set of
  goals with checkable outcomes (file contents, exit codes) and track pass rate,
  steps/run, tokens/run across commits. This is the same muscle the triage
  harness needs (macro-F1, grounding, trajectory efficiency), and the single most
  differentiating thing you can put on a resume.

## S2 notes

Built: runs execute in `kama-core`; `kama run` / `attach` / `runs` / `cancel` are clients;
text streams live as `llm.delta`; approvals go over IPC; token auth.

Design choices worth defending:
- **Durable vs ephemeral events.** Every event with a `seq` is persisted and replayable.
  Streamed text isn't: it's high-volume and redundant with `llm.response`, so it's
  broadcast live only. A reconnecting client loses in-flight text, never state.
- **Replay then live, without gaps.** A subscriber gets the stored events from `from_seq`,
  then live ones. The backlog snapshot and the subscriber registration happen with no
  `await` between them, and asyncio is single-threaded, so nothing can fall in between;
  live events already in the backlog are skipped by `seq`.
- **Backpressure: the run never waits.** Each subscriber has a bounded queue (1000). On
  overflow the daemon drops that subscriber's queue and ends its stream with
  `lagged` + `next_seq`; the CLI re-subscribes from there. The alternative (block the
  run on the slowest client) lets one stuck terminal stall an agent.
- **Approvals as data.** `tool.approval_requested` / `tool.approval_resolved` are durable
  events, so the log shows who approved what and how long it took. First answer wins;
  unanswered requests become a denial after `KAMA_APPROVAL_TIMEOUT_S`.
- **Ctrl+C vs closing the terminal.** Ctrl+C on `kama run` cancels the run (explicit
  intent). A dropped connection does not: `kama attach` picks the run up again.
- **Auth.** Token in a 0600 file, required on every connection. 127.0.0.1 is reachable by
  every local process and, via DNS rebinding, by web pages.

Bugs found while building (all have tests now):
- Subscriber was a dataclass, so unhashable; adding it to a set crashed the subscription
  task *silently*, which showed up as a hang. Fix: identity hashing, plus a done-callback
  that logs any crashed connection task, plus pytest-timeout.
- A second daemon that failed to bind had already overwritten the token file, locking
  clients out of the running daemon. The token is now written only after bind.
- The streaming callback closed over the loop variable `steps` (ruff B023): deltas could
  be tagged with the wrong step. Bound as a default argument.

Still open: token counts / latency per span are in events but there's no trace view yet
(next: Trace). The eval harness still runs in-process via `run_goal`.

## Interview talking points

### S1
- "Walk me through your agent loop": the stop-reason state machine above, the append-only
  history, why tool errors are results and API errors end the run.
- A bug found by running the CLI rather than the tests: with no credentials the SDK raises
  a bare `TypeError`, not an API error. It got past the error mapping, and the run ended
  without `run.finished`. The fix was to map that case explicitly and catch any other
  exception at the loop level, so the invariant holds whatever happens.
- Testing without the network: a scripted fake LLM for loop logic, plus the real provider
  against a mocked HTTP transport to check the JSON actually sent (tool_result ids,
  verbatim thinking-block echo).

### S0

- Why a daemon + thin clients instead of a script: runs survive client crashes, multiple
  frontends share one event stream, and permission prompts can be routed to whichever
  client is attached.
- Why NDJSON over TCP: trivially debuggable (`nc localhost 7437`), language-neutral, and
  framing is one `readline` call. Trade-off: no multiplexing or streaming built in, so S2
  has to add event subscription on top.
- Correct JSON-RPC error semantics (-32602 for bad params vs. -32600 for a bad envelope),
  a bounded frame size, and no exception text leaking to clients. The reference S0 maps
  bad params to -32600 and catches `LimitOverrunError`, which `readline()` never raises
  (it raises `ValueError`). Being able to say this shows you read the spec and the stdlib.
- A readiness race found by a test: the daemon announced "listening" before installing
  signal handlers, so an early SIGTERM killed it uncleanly. Readiness must be signalled
  only once the process can handle shutdown.

## S1 notes

Design choices worth being able to defend:

- **Manual loop instead of the SDK's tool runner.** The runner is shorter, but the point
  here is to own the mechanics: stop reasons, the order of tool results, what gets echoed
  back, and where approval sits. The runner is also a beta API.
- **History is kept in the wire format, not our own message classes.** Assistant blocks
  must come back byte-for-byte (a thinking block's signature is checked server-side), so
  converting them to our own types and back is a way to introduce bugs. `LLMResponse` stores
  the raw blocks and offers parsed views (`text`, `tool_calls`).
- **Stop reasons map to run outcomes:** `end_turn` → completed, `tool_use` → keep going,
  `max_tokens` → truncated, `refusal` → refused, anything unknown → error. `max_steps` is
  our own safety limit.
- **Errors split in two.** Tool errors go back to the model as `is_error` results so it can
  adapt. API errors end the run, because the SDK has already retried 429/5xx twice.
- **Tools run one after another, not concurrently,** even when the model asks for several
  at once. Approval prompts are interactive, and a write followed by a read in the same turn
  must not race. Results still go back in a single message.
- **Prompt caching from step 1.** Every step resends the whole history, so without caching
  the input cost grows quadratically with the number of steps. `cache_read` is printed
  per step so you can see it working.

Known gaps, each owned by a later stage:
- No streaming yet. Each step is one non-streaming call with max_tokens 16k (S2 streams tokens as events).
- Approval is a yes/no prompt per call, with no policy (S5).
- The context grows without bound (S6).
- The run log sits inside the workspace (`.kama/runs`), where the agent can see it.

### Eval seed: harness and 8 tasks written; full baseline next (before S2)

`evals/` has the harness and 8 tasks (see the suite table in docs/EVALS.md). Baseline on
the first three: 9/9 (saturated; they are now regression tasks). Next: run all 8 × 3,
read the failing traces, and record the baseline before starting S2.
