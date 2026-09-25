# Roadmap

Stages mirror the reference project's `stage/s*` branches. Each stage ends with
something runnable and a test that proves it. **Done** means `make verify` passes and
the demo command works, not "the code is written".

| Stage | Build | Done when | Fundamental it teaches |
|---|---|---|---|
| **S0** ✅ | CLI ↔ daemon over JSON-RPC 2.0 / NDJSON / TCP; typed protocol; config | `kama ping` returns pong from a separately running `kama-core`; error codes tested | Process boundaries, wire contracts, asyncio streams |
| **S1** | `kama run "<goal>"`: agent loop (LLM → tool_use → tool_result → …) with read_file / list_dir / write_file / bash; every step appended to `runs/<id>/events.jsonl` | A real goal completes end to end; loop unit-tested against a scripted fake LLM | Raw Messages API mechanics: tool schemas, stop reasons, message assembly |
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

## Interview talking points already earned in S0

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
