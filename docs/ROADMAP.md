# Roadmap

Stages mirror the reference project's `stage/s*` branches. Each stage ends with
something runnable and a test that proves it. **Done** means `make verify` passes and
the demo command works, not "the code is written".

| Stage | Build | Done when | Fundamental it teaches |
|---|---|---|---|
| **S0** ✅ | CLI ↔ daemon over JSON-RPC 2.0 / NDJSON / TCP; typed protocol; config | `kama ping` returns pong from a separately running `kama-core`; error codes tested | Process boundaries, wire contracts, asyncio streams |
| **S1** ✅ | `kama run "<goal>"`: agent loop (LLM → tool_use → tool_result → …) with read_file / list_dir / write_file / bash; every step appended to `runs/<id>/events.jsonl` | A real goal completes end to end; loop unit-tested against a scripted fake LLM | Raw Messages API mechanics: tool schemas, stop reasons, message assembly |
| **S2** ✅ | Move the runner into the daemon; clients subscribe to an event stream over IPC | Two clients watch the same run live; client crash doesn't kill the run | Pub/sub, backpressure, cancellation in asyncio |
| **Trace** ✅ | Span-level trace of IPC → event bus → LLM calls (latency, tokens, cost) | You can replay a run and say where the time and tokens went | Observability: the same idea as Langfuse/LangSmith, built by hand first |
| **S3** ✅ | Task tools (create/update/get/list, dependencies) so the model plans; the user can steer the plan; TUI | A multi-step goal shows a visible plan being executed, in the CLI and the TUI; the eval A/B says whether planning helps | Planning as tools, not prompts; a real frontend over the protocol |
| **S4** ✅ | Sessions: multiple runs share a thread (history replayed from events, interrupted runs repaired); durable notes (workspace/session scope, provenance, volatile values); `kama chat`, notes in CLI/TUI | Run 2 uses a fact learned in run 1 without re-reading it; memory never makes the agent trust stale data (eval: 3 multi-run tasks) | Memory tiers: working context vs. session history vs. durable notes |
| **S5** | Tool safety: param validation, permission policy + approval flow, failure classification, retry | A denied `bash rm` is blocked and the model recovers; transient errors retry, permanent don't | Failure handling for agents |
| **S6** | Context governance: token budget, tool_result truncation, compaction | A long session stays under budget with measured quality loss | Context engineering, token accounting |
| **S7** | Skills, subagents, MCP client | An MCP server's tools appear in the registry and get called | Extension boundaries |

## Priorities given the actual goal

This project exists to feed the **incident-triage agent over ELK** and a finance-agent
job search. That changes the priority order:

- **No stage is timeboxed or cut.** Every stage is built to its fullest version (decided
  during S3; this replaced an earlier "timebox the TUI and S7" rule).
- **Most reused later:** S1, Trace, S5, S6: the agent-loop, observability, failure-handling
  and context skills the triage agent reuses directly and interviewers ask about.
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

## Trace notes

Built: a hand-rolled tracer (same data model as OpenTelemetry / Langfuse / LangSmith),
spans across three layers (LLM, agent + event bus, IPC), `kama trace` (breakdown,
tokens, cache hit, cost, waterfall, slowest spans, per-client delivery lag, per-method
IPC latency) and a Perfetto export.

Design choices worth defending:
- **Spans vs events.** Events record what happened (for replay); spans record how long
  each part took and inside what (for performance). Same run, two questions, two files.
- **ContextVar for parent links.** A global "current span" breaks as soon as two asyncio
  tasks run concurrently; each task gets its own context copy, so a task created inside
  a span still sees the right parent and siblings never nest.
- **Wall-clock start, monotonic duration.** Wall-clock places spans on a timeline;
  monotonic durations can't go negative when NTP adjusts the clock.
- **TTFT measured in the provider, at the first content delta** (not message_start,
  which arrives before generation). TTFT separates "slow to start" from "wrote a lot";
  tokens/s is computed over generation time only.
- **Pure analysis.** `summarize()` takes spans and returns numbers, so its maths is tested
  with hand-made spans and exact values; rendering is separate.
- **Cross-trace links.** A run started from an IPC request is linked to it
  (`linked_span`) instead of parented, so each trace file stands alone.
- **Unknown cost is None, not $0.** A zero cost looks like data and hides a missing price.

What the trace found (the point of building it):
- `run.start` took 83 ms. Measured the cause: constructing an Anthropic SDK client costs
  50-80 ms, and the daemon built one per run (also losing the connection pool, so each
  run's first call paid a TLS handshake). Fix: one shared client per API key, built at
  startup. Result: 79.7 ms -> 0.94 ms, and 2.7 ms on the first run after a restart.

Bugs found by the new tests:
- Subscription spans recorded the client as `?`: the client name was never passed
  through. The test asserting both client names caught it.
- `Tracer.record(**attrs)` let an attribute dict collide with named parameters
  (`error`, `parent_id`); mypy flagged it. It now takes an explicit `attrs=` mapping.

Still open: spans stay local (JSONL). Exporting to Langfuse/OTLP is a thin adapter over
the same Span model when it's needed; the eval harness could record per-trial TTFT.

## S3 notes

Built (full version):
- **Plan as state:** a per-run task DAG (`blocked_by`, cycle-checked; a task can't start or
  complete while a blocker is open), descriptions, timestamps; tools `task_create`,
  `task_update` (batched, all-or-nothing, applied in order), `task_get`, `task_list`.
- **Runtime behaviour on top of it:** `plan.updated` snapshots, one `plan.reminder` when
  the model stops with open tasks, and a step allowance: plan-only steps don't count
  against `max_steps` (up to `max_steps // 2`).
- **Steering:** `plan.get` / `plan.edit` over IPC, `kama plan show|add|cancel`, and in the
  TUI. An edit reaches the model on its next unsent user message (`plan.notice`).
- **Observability:** plan progress in `kama runs`, a plan line, plan-only steps and "time
  per task" (one `plan` span per stretch of work) in `kama trace`, its own Perfetto lane;
  plan metrics, budget credit and an "ended at max_steps" count in the eval summary.
- **TUI (`kama tui`):** status line, live log (streamed text under its step, collapsible
  tool calls), live plan panel, goal box, approval modal, steering, stop, runs picker,
  trace view, and reconnect-with-resume.
- **Eval:** `risk-report-spec` (ten requirements graded separately); `KAMA_PLANNING=false`
  is the S2 agent byte for byte, for A/B runs.

Method, as in S1: the eval task first (what "done" means), then plan state, tools, loop,
daemon, CLI, trace, harness, TUI, each tested with scripted fakes before the next layer;
then real processes against `scripts/fake_api.py` (automated integration tests for both
the CLI and the TUI); then these notes.

Design choices worth defending:
- **Planning as tools, not prompts.** A plan in prose scrolls away and nothing can check
  it. As state, the runtime can show it, persist it, enforce the order the model itself
  declared, time each task, and notice "said done with work open".
- **CRUD by id, batched** (vs. TodoWrite's full rewrite): an update costs a few tokens, a
  task can't silently vanish (cancel needs a reason), and "complete 1, start 2" is one
  atomic call.
- **Dependencies are enforced**, not advisory: starting a blocked task is an `is_error`
  result naming the open blockers and how to drop the dependency. A cancelled blocker
  counts as resolved.
- **Snapshots, not diffs, in events**: a late client needs only the last one; replaying
  one twice is harmless. Clients compute diffs for display.
- **One reminder, never more**, and **steering notices go on the next unsent user message**:
  both keep history append-only, and both are durable events, so `events.jsonl` still
  rebuilds exactly what the model saw (tested for both).
- **Attribution:** only a plan tool may emit a model-attributed plan change. A user edit
  that lands while `bash` runs bumps the plan version; without that rule it would be
  reported as a change made by `bash` (found while designing, pinned by a test).
- **The step allowance** comes from the Haiku A/B (below): with the same `max_steps`, the
  planning agent spent budget on bookkeeping that the no-plan agent spent on work. The
  allowance gives both arms the same work budget; the cap bounds a bookkeeping loop.
- **The TUI folds events into a pure `RunView`** and only renders; dedupe on reconnect,
  cost, plan and pending approvals are unit-tested without a terminal.

A/B results (user's VM, Haiku 4.5, 3 reps × 9 tasks; `evals/results/kama-run/`):

| | round 1 (no allowance) | round 2 (with allowance) |
|---|---|---|
| no plan | 23/27 (`s3-noplan-haiku`) | 22/27 (`s3b-no-plan`) |
| plan | 23/27 (`s3-plan-haiku`) | 24/27 (`s3a-plan-haiku`) |

- **Pass rate: no measurable effect** (differences within the ±19% noise floor). Opus 5 is
  at the ceiling (27/27 without planning), so the suite can't tell either way for it.
- **The step allowance did what it was built for.** Round 1: the four "open tasks at the
  end, no reminder" trials all ended at `max_steps`, and `rename-across-files` dropped to
  1/3 with 8-9 of its 20 steps spent on bookkeeping. Round 2: 3/3, with 9-10 steps credited.
  Cost per trial ends up about equal ($1.22 vs $1.25 per 27 trials).
- **The reminder never fired** in 54 planning trials. The failure it was designed for
  (saying "done" with work open) didn't happen; the one that did was budget exhaustion.
- **Planning fixes completeness, not comprehension.** `risk-report-spec` failed R8/R9 in
  every Haiku trial, with or without a plan: it summed realized P&L over *open* positions,
  dropping fully closed ones (KO), so 39.5 instead of 177.0 on the visible data. The plan
  faithfully contained the wrong interpretation. Opus spotted the same trap unprompted.
  Now a `wrong/realized-over-open-positions` answer, and the checker's reason names the
  field: `R8: realized_pnl 1120.0 != 1270.0`.
- **`cleanup-trap` (plan 6/6, no plan 3/6) is confounded:** 4 of the 6 planning-arm trials
  never made a plan, so the difference is the prompt/tool list or chance, not planning. One
  no-plan failure deleted `.git`, which is S5's job to block.
- **Decision:** planning stays on by default: neutral on pass rate, cost-neutral with the
  allowance, and the plan panel and steering need it.

Bugs found by the tests and the screenshots:
- The runs picker sorted by run id, which has 1-second resolution plus a random suffix,
  so two runs started in the same second came out in random order (test flaked; now
  sorted by `started_at`).
- Naming a TUI helper `_log` silently overrode Textual's own `App._log`; mypy's override
  check caught it.
- From the screenshots, not the tests: streamed text appeared under the previous step
  (deltas arrive before the llm.response that drew the divider) and the log didn't scroll
  to new content (scrolled before layout). Both fixed; ordering now tested.
- A test "dropped" a TUI connection that wasn't open yet (the run reached its pause before
  the watch connected); several expectations of mine were wrong (version counts, what
  counts as a plan-only step, when a mid-call edit reaches the model: the *next* call).

### Re-running the S3 experiment (VM; costs money)

```bash
uv run python -m evals.run_evals run --approve-harness --reps 3 --variant s3b-plan-haiku --model claude-haiku-4-5
KAMA_PLANNING=false uv run python -m evals.run_evals run --reps 3 --variant s3b-noplan-haiku --model claude-haiku-4-5
uv run python -m evals.run_evals summary --variant s3b-plan-haiku    # and s3b-noplan-haiku
```

Read, in this order: pass rate against the noise floor, "ended at max_steps", the
planning line (plans made, open at end, passed after reminder, plan-only steps and how
many were credited), `risk-report-spec`'s per-requirement reasons, then cost per trial.

## S4 notes

Built (full version):
- **Three memory tiers.** Working context (one run's messages); session history (earlier
  runs of the same conversation, replayed); durable notes (facts saved for later runs,
  workspace scope across sessions or session scope).
- **Sessions** (`core/session.py`): a JSON file listing a session's runs. The history is
  rebuilt from each run's `events.jsonl` (`core/agent/history.py: replay`), never stored
  twice. `run.start` takes `session_id` / `new_session`; one run per session at a time;
  sessions survive a daemon restart.
- **Notes** (`core/notes.py`, `note_save/update/delete/list`): source, volatile flag,
  author, run; bounded per scope; `note.updated` events.
- **The memory block**: at run start, notes (and, for a continued session, when the last
  run ended and that the workspace may have changed since) go before the goal in the
  first user message.
- **Clients**: `kama chat` (a conversation, `/notes`, `/note`, `/new`), `kama session`,
  `kama notes`, `run --session/--new-session`; the TUI continues conversations (ctrl+n
  starts a new one), shows a live memory panel and manages notes (ctrl+l).
- **Observability**: `run.started` records session, history size, repairs and the memory
  block; `kama trace` gets a memory line; eval rows get memory metrics.
- **Eval, written first**: multi-run tasks in the harness (`[[runs]]`, `between()` hook,
  per-run records so checks can grade the trajectory), and three tasks:
  `recall-across-runs` (session history), `workspace-notes` (notes across sessions),
  `stale-fact` (a guard: memory must not make the agent use a rate that has changed).

Design choices worth defending:
- **History is rebuilt from events, not stored as messages.** One source of truth: the
  loop builds its messages with the same helpers `replay` uses, and a test checks that
  the rebuilt conversation equals what was sent. A second messages file (the reference's
  choice) can disagree with the runs it summarizes.
- **Interrupted runs are repaired on the way in.** A run cancelled mid-tool leaves
  tool_use blocks without results (possibly some answered, some not); the API rejects
  that. The next run adds is_error results ("the previous run ended before this tool
  call finished") and records `repaired=N` in its run.started. A trailing user turn
  (max_steps after tools, an API error) is joined, never doubled. `conversation_problems()`
  checks the API's structural rules locally and is used on every rebuilt history in tests.
- **Notes go in the first user message, not the system prompt.** The system prompt stays
  byte-stable (cache prefix), and a continued session's earlier turns are reused as a
  cache prefix too. The reference injects notes into the system prompt, which re-caches
  everything whenever a note changes.
- **Workspace and session scope.** Project facts (how to run the tests) belong to the
  workspace and must survive a new conversation; conversation details shouldn't leak
  into the next one (tested both ways).
- **Memory is the past.** Notes carry a source and age; volatile ones are flagged for
  re-checking; a continued session is told how long ago it last ran. Memory poisoning (a
  file's text saved as a note and replayed into every later run) is mitigated by framing
  notes as the agent's own observations with their source, not as instructions, and by
  letting the user list and delete them.
- **KAMA_MEMORY=false is the S3 agent, byte for byte** (no history, no notes, same prompt),
  so the memory A/B changes one thing.

Bugs found by the tests (and one by looking at the machine):
- Tests were writing into the real `~/.kama` (a `runs/_daemon` folder left over from an
  earlier stage). Every test now gets its own HOME.
- My expectation of *where* the repair happens was wrong: `history()` is what happened
  (orphans included); the run that continues repairs it and says so. That is the better
  design, so the test changed, not the code.
- Thinking through cancellation exposed partially answered tool turns (one tool finished,
  the next was cancelled): the repair originally only handled a bare tool_use turn.
- The eval harness marked memory-off trials as having memory metrics (they still use
  sessions to group runs); it now follows the setting, not the events.
- ruff caught an assert that could never fail (`assert "a" "b" in x` without parentheses
  asserted only the string); mypy caught a `list` method shadowing the builtin in
  annotations; two traps in the recall task produced the same wrong answer (fixed so each
  trap is distinguishable).

### The S4 experiment (VM; costs money)

```bash
uv run python -m evals.run_evals run --approve-harness --reps 3 --variant s4-mem \
  --tasks recall-across-runs,workspace-notes,stale-fact
KAMA_MEMORY=false uv run python -m evals.run_evals run --reps 3 --variant s4-nomem \
  --tasks recall-across-runs,workspace-notes,stale-fact
uv run python -m evals.run_evals run --reps 3 --variant s4-full     # regression: all 12
```

Predictions, written before the run so the result can prove them wrong:
`recall-across-runs` passes with memory and fails without (run 2 has to re-read or ask);
`workspace-notes` fails without memory by construction, and with memory passes only if
the model chose to save the command in run 1 (the real question); `stale-fact` passes in
both arms, and a memory-arm failure there is the most important result of the stage.

### S4 results, round 1 (Opus 5, effort high, 3 reps; `compare s4-mem s4-nomem`)

| task | check | memory | no memory |
|---|---|---|---|
| recall-across-runs | **passed** | 0/3 | 0/3 |
| | answer (ARCX/R07) | **3/3** | 0/3 |
| | run 1 wrote nothing | 2/3 | 3/3 |
| | run 2 didn't re-read the log | 0/3 | 0/3 |
| stale-fact (guard) | **passed** | 3/3 | 3/3 |
| workspace-notes | **passed** | 0/3 | 0/3 |
| | run 2 didn't open CONTRIBUTING.md | 0/3 | 0/3 |
| | run 2's first test run was right | 3/3 | 2/3 |
| all three | median cost per trial | $0.16 | $0.21 |

3/9 vs 3/9, and the pass rate says nothing about what happened:
- **Memory carried the answer.** Without it, run 2 ("record that") had no idea what
  "that" was: it re-derived from the whole log and fell into the planted trap
  (XNAS/R15, the whole-file answer) twice, and refused to guess once. With memory it was
  right 3/3. Memory was also cheaper overall ($1.31 vs $1.81, median 10 vs 12 steps).
- **The guard held.** stale-fact passed 3/3 with memory: every run 2 re-read the rate
  and said it had changed (1.0850 → 1.0920).
- **Two predictions were wrong, and my prompt is the reason.** With memory, run 2 still
  re-read the log ("re-verified against logs/oms.log first") and the docs ("note w1 still
  matches the source") every time. The continuation line told it to "re-check files and
  values before relying on what earlier turns say about them": a blanket rule, written
  for the stale-fact guard, that also forbids what recall-across-runs rewards. The model
  did what it was told. The volatile flag exists to draw that line, and the prompt didn't
  use it.
- Saving notes was never the problem: notes in 9/9 memory trials, 3 of them volatile
  (all the FX rate).
- Two side findings. (1) One memory run 1 wrote a scratch file (`logs/r15.txt`) despite
  "don't create or change any files". (2) Without memory, run 2 of stale-fact queried a
  live FX API from bash (frankfurter.dev, open.er-api.com) to flag that the 2024 rate is
  old. The answer was still right, but an eval agent reaching the internet is an
  uncontrolled input, and an S5 egress-policy case.

**Round 2: one change.** The memory wording now ties trust to volatility. Reuse what
earlier turns established about fixed inputs (a past day's log, a spec, a test
command); use notes instead of rediscovering them; re-check only volatile values, or a
file when something suggests it changed (`core/notes.py: memory_preamble`,
`agent/prompts.py: _MEMORY`). `KAMA_MEMORY=false` is unchanged, so the no-memory arm
needs no re-run. Predictions: recall-across-runs and workspace-notes rise to ≥2/3
each. **stale-fact must stay 3/3**: this wording is the one that could let a remembered
rate through, and a failure there means the line moved too far.
The risk to name: the wording is tuned on the very tasks that measure it. The
`s4-full` regression (all 12 tasks) is the check that it changed nothing else.

```bash
uv run python -m evals.run_evals run --approve-harness --reps 3 --variant s4-mem2 \
  --tasks recall-across-runs,workspace-notes,stale-fact
uv run python -m evals.run_evals compare s4-mem s4-mem2
uv run python -m evals.run_evals run --reps 3 --variant s4-full     # regression: all 12
```

### S4 results, round 2 (same model and effort; harness 7d110c02 for both variants)

`s4-mem2` (the three memory tasks) and `s4-full` (all 12) ran the same code, so their
memory-task trials pool to 6 per task. Round 1 had 3 per task.

| check | round 1 (old wording) | round 2 (pooled) | one-sided Fisher p |
|---|---|---|---|
| recall: run 2 didn't re-read the log | 0/3 | 4/6 (3/3 + 1/3) | 0.12 |
| workspace-notes: run 2 didn't open the docs | 0/3 | 6/6 | 0.012 |
| both together | 0/6 | 10/12 | 0.0015 |
| stale-fact (guard) | 3/3 | 6/6 | held |

- **The change worked, and the guard held.** Run 2 now says "values reused from the
  earlier analysis of the (fixed, past-day) log", and every stale-fact run 2 still
  re-read the rate. All three predictions held.
- **It is not deterministic.** In `s4-full`, 2 of 3 recall run 2s re-confirmed anyway
  ("re-confirmed … 410 vs XNAS 390"). 3/3 in one variant and 1/3 in the next, with
  identical code, is the noise floor in action: one 3-rep run would have told either
  story. Notably the margin is close (410 vs 390, 5%), and re-checking a close call
  before writing an incident record is defensible. The grader calls it a failure
  because the task was built to measure reuse, not because it is wrong.
- **Regression: nothing broke.** `s4-full` 34/36. The 9 tasks shared with
  `s3-noplan` are 27/27 in both. cleanup-trap is 3/3 (no `.git` deletion this time).
- **The cost is real.** On those 9 shared tasks, `s4-full` cost $3.48 vs $2.86 (+22%),
  180 vs 158 tool calls. Two causes, confounded: `s3-noplan` had planning off, and
  `s4-full` has both planning and memory on (8 more tool specs, a longer system prompt,
  5 plans made). Notes were saved in 15 of 27 single-run trials (cleanup-trap 2-3 each),
  and those notes are never read in a fresh eval workspace. Even tasks that saved
  nothing cost more on the first, uncached call (fix-add-bug $0.028 → $0.048). The
  clean split needs `KAMA_MEMORY=false` on all 12 with planning on; see below.
- **Decision:** memory stays on with the round-2 wording. For memory-shaped work it
  turned a wrong answer (0/3) into a right one (3/3) at lower cost. For single-shot work
  it is overhead. S6 (context governance) is where per-run tool and prompt cost gets
  managed.

Optional, to separate memory's cost from planning's (about $4, 36 trials):

```bash
KAMA_MEMORY=false uv run python -m evals.run_evals run --reps 3 --variant s4-full-nomem
uv run python -m evals.run_evals compare s4-full-nomem s4-full
```

## Interview talking points

### S4
- "How does your agent remember?" Three tiers, each with a different lifetime and
  failure mode; history rebuilt from an event log (one source of truth); notes with
  provenance and a volatile flag; memory framed as observations, not instructions.
- The stale-data guard: in finance a remembered price is a liability. The eval has a task
  whose only purpose is to catch memory making the agent wrong.
- The A/B where pass rate lied: 3/9 vs 3/9, but per-check grading showed memory took the
  answer from 0/3 to 3/3 and cut cost ~28%. The "failures" were the model obeying my
  own blanket "re-check everything" instruction: a trust-vs-verify policy has to be
  explicit (here: by volatility), or the safety rule silently cancels the feature.
  Round 2 made it explicit: re-reads went from 0/6 to 10/12 (p≈0.002), and the stale-data
  guard held 6/6. The same code scored 3/3 and 1/3 on one check in two runs: that is
  why I pool runs and quote exact p-values rather than one 3-rep result.
- Correctness of resumed conversations: repairing interrupted tool calls, never doubling
  user turns, and a local validator for the API's conversation rules.


### S3
- "How does your agent plan?" Tools, not prompts: a task DAG the runtime can check,
  enforce, time and show, and that a human can steer mid-run.
- The measurement story: a one-variable A/B, an eval that grades ten requirements
  separately, and cost metrics next to benefit metrics. Planning did not move the pass
  rate; the hypothesis behind the reminder was wrong (it never fired); the real cost was
  step-budget exhaustion, which led to the step allowance.
- Human-in-the-loop without breaking the conversation: edits land on the next unsent
  user message, recorded as events, never rewriting history.

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
