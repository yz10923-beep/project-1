# Roadmap

Stages mirror the reference project's `stage/s*` branches. Each stage ends with
something runnable and a test that proves it. **Done** means `make verify` passes and
the demo command works, not "the code is written".

| Stage | Build | Done when | Fundamental it teaches |
|---|---|---|---|
| **S0** ✅ | CLI ↔ daemon over JSON-RPC 2.0 / NDJSON / TCP; typed protocol; config | `kama ping` returns pong from a separately running `kama-core`; error codes tested | Process boundaries, wire contracts, asyncio streams |
| **S1** ✅ | `kama run "<goal>"`: agent loop (LLM → tool_use → tool_result → …) with read_file / list_dir / write_file / bash; every step appended to `runs/<id>/events.jsonl` | A real goal completes end to end; loop unit-tested against a scripted fake LLM | Raw Messages API mechanics: tool schemas, stop reasons, message assembly |
| **S2** | Move the runner into the daemon; clients subscribe to an event stream over IPC | Two clients watch the same run live; client crash doesn't kill the run | Pub/sub, backpressure, cancellation in asyncio |
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

### Eval seed: harness built, 3 of ~8 tasks written (finish before S2)

`evals/` has the harness and three seed tasks (`fix-add-bug`, `vwap-cli`,
`clarify-vague-goal`). See docs/EVALS.md. Still to write: a multi-file edit, a refactor
that must keep the tests green, a destructive trap ("clean up this repo"), large output
that exercises truncation, a first approach that fails and needs recovery. Then record
the baseline: 8 tasks × 3 reps.
