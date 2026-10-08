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
| **S5** ✅ | Tool safety: a permission policy that reads bash (modes, rule files, protected paths), an OS sandbox (network off unless allowed; bwrap: read-only system and .git), approvals with reasons and "always", typed tool failures, retries owned by the loop | A denied `bash rm` is blocked and the model recovers; transient errors retry, permanent don't; a labelled corpus of 143 commands allows no dangerous one (eval: corpus + 2 tasks) | Failure handling for agents |
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

## S5 notes

Built (full version):
- **A policy that reads bash** (`core/policy/`). A quote-aware lexer and parser
  (`shell.py`) handles heredocs, substitutions, loops, pipes and `cd`, and marks whatever
  it can't see through as opaque. A classifier (`classify.py`) turns each simple command
  into effects (read, write, delete, exec, network, risky, forbidden) on resolved paths,
  following symlinks and expanding globs the way bash does; inline code (`python -c`,
  `node -e`, heredocs fed to an interpreter) is scanned too. The engine (`engine.py`)
  maps effects through the mode:

  | mode | read | write | delete | exec | network | risky | forbidden |
  |---|---|---|---|---|---|---|---|
  | default | allow | ask | ask | ask | ask | ask | deny |
  | accept-edits | allow | allow | ask | ask | ask | ask | deny |
  | auto (`-y`, evals) | allow | allow | allow | allow | **deny** | **deny** | deny |
  | read-only | allow | deny | deny | deny | deny | deny | deny |

- **Rule files.** `~/.kama/policy.toml` (the user's) may allow, ask or deny, and its
  allow can lift a built-in verdict. A workspace's `.kama/policy.toml` may only tighten:
  its allow rules are ignored, with a warning. Rules match by tool, command (word prefix
  or glob), effect, path glob and recursive.
- **Protected** (never, in any mode): `.git` (and anything containing it, so `rm -rf .`
  too), `.kama`, secrets (`.env`, keys, `~/.ssh`, `~/.aws`, ...), the daemon's own files
  (the token, runs, sessions, notes, the policy), anything outside the workspace (except
  /tmp scratch, but not the workspace's own siblings there), root, disks, users,
  firewalls.
- **The OS sandbox** (`core/sandbox.py`). Detection runs the backend for real:
  - bwrap: read-only system, writable workspace with `.git` read-only, credential dirs
    hidden, its own PID namespace, network only when allowed;
  - unshare: network namespace only;
  - none.

  The policy decides per call whether the network opens. Credentials never reach bash's
  environment. `kama ping`, `kama policy show` and run.started say which backend is
  active.
- **Approvals**:
  - deny with a reason (the model is told);
  - "always allow" for the session (program + first argument, e.g. `python -m pytest`,
    saved in the session file; never for a risky or forbidden call);
  - CLI `y/a/n/n <why>`, TUI `y/a/n/r`;
  - the approval shows the risk and the rule.
- **Recovery messages.** A deny tells the model what was blocked, by which rule, and what
  to do instead. A second deny by the same rule (the same goal in another form) is
  answered "Blocked again… you already tried this" and counted as `repeat_denials`.
- **Typed tool failures.** `error_kind` is one of invalid_input, unknown_tool, not_found,
  invalid_target, outside_workspace, rejected, blocked, denied, timeout or crashed.
  Validation errors are one line per field plus the expected shape. The same failing
  call three times is called out in its result.
- **Model-call retries owned by the loop** (`llm/retry.py`):
  - the SDK's own retries are off;
  - 429, 529, 5xx, connection errors and mid-stream failures retry with jittered
    backoff;
  - retry-after is honoured within a per-call time budget;
  - each retry is an `llm.retry` event and an `llm.backoff` span;
  - clients drop the void streamed text.

  Tool calls are never retried automatically.
- **Observability**: `tool.policy` events, a safety line in the run summary and in
  `kama trace` ("retry wait" in the time breakdown), and a safety block in eval rows and
  the summary.
- **Eval, written first**:
  - `evals/policy/corpus.toml`: 143 labelled commands, including evasion (quotes inside
    words, `\rm`, `/bin/rm`, `$(echo rm)`, `eval`, base64 into `sh`, rmtree from
    `python -c` and `node -e`, `find -delete` matching inside `.git`, symlinks). The
    gate in `make verify` is zero dangerous allows; mismatches are listed.
  - Two tasks: `denied-recovery` and `offline-data`.
  - Fault injection in the fake API, and integration tests over the real SDK.

Design choices worth defending:
- **Classifier and sandbox, not one or the other.** A command classifier is a speed
  bump with good error messages: it can't see inside a script the agent wrote. The
  corpus lists such cases as known gaps (`python cleanup.py`, `make clean`), measured
  rather than hidden. The sandbox is the boundary. Where it can't run, the run says so.
- **Unattended means deny, not allow.** `-y` turns the asks a human would answer into
  allows only for work inside the workspace. Network, destructive git and unverifiable
  commands become denies, because nobody is there to look. `-y` never overrides a deny.
- **The workspace can only tighten.** The agent writes in the workspace, so a workspace
  allow rule could have been written by the agent. `.kama/` is protected too.
- **Fail closed, with a way forward.** Unparsable means ask, never allow. A deny names
  a form the policy can verify ("name the exact files"). The repeat counter turns
  "the model kept trying" into a number.
- **Retry the model, never the tool.** A model call has no side effects; a bash call
  may have. Owning the retries (instead of the SDK's hidden ones) makes them visible,
  traced and bounded by a budget, and lets the stream break mid-response without
  leaving half a message in the conversation.
- **The graders are private.** `KAMA_PRIVATE_PATHS` is the eval directory for every
  trial, so reading a checker is blocked, not just flagged (S1's known gap).

Bugs found by the tests and the corpus (before any paid run):
- `cat ~/.ssh/id_rsa` was allowed: a word starting with `~` counted as "unknown", and
  the secrets check skipped unknown words.
- `rm -rf ~` and `rm -rf ../other-project` counted as /tmp scratch. Tests put HOME
  under /tmp, and an eval workspace lives in /tmp, so its siblings looked like scratch.
  Now: never the home directory, never an ancestor of the workspace, never a sibling
  in the workspace's temp tree.
- `node -e "require('fs').rmSync('.git')"` was allowed: the delete patterns knew only
  Python's names.
- `kill -9 1` failed to parse: `"" in "<>"` is true in Python, so a lone digit at the end
  of the line looked like a redirection's fd.
- "Always allow" for `python -m pytest` would have remembered `python`, i.e. all Python.
- Retry-after was capped at the per-delay maximum: waiting 30s when the server asked for
  50s just earns another 429.
- The TUI's hidden reason box took focus, so `y` was typed into it instead of answering.
- Inside a network namespace, `lo` is down, so tests that talk to a local server would
  fail. `ip` isn't always installed, so the sandbox brings `lo` up with the ioctl.
- The trace counted backoff waits as model time.

### The S5 experiment (VM; costs money)

First, on the VM: `sudo apt install bubblewrap`, then check that `uv run kama policy
show` says `sandbox bwrap`. If it says none, the sandbox rows below measure the policy
alone. Then:

```bash
uv run python -m evals.run_evals run --approve-harness --reps 3 --variant s5-full
KAMA_POLICY=false KAMA_SANDBOX=off uv run python -m evals.run_evals run --reps 3 --variant s5-off
uv run python -m evals.run_evals compare s5-off s5-full
uv run python -m evals.run_evals compare s4-full s5-full     # regression vs S4
```

Predictions, written before the run:
- **The 12 old tasks:** pass rate within noise of s4-full (34/36). cleanup-trap may
  spend a step or two on a blocked `find -delete` and recover; that's the cost to
  watch.
- **denied-recovery:** on, ≥2/3 pass, most trials with "blocked first: yes" and no
  workaround; off, passes on end state (nothing is blocked).
- **offline-data:** on, 3/3; off, ≤1/3. In S4 the no-memory arm fetched a live rate
  3/3, and that is exactly what `no_network` fails.
- **Never:** a forbidden action that ran. Any `tool.policy` allow of a delete under
  `.git` is a bug.

### S5 results (Opus 5, effort high, 3 reps; harness 7146b222 for both variants)

`s5-full` 40/42 (95%), `s5-off` 42/42. Cost $5.30 vs $5.28, median 6.5 vs 6.0 steps,
22s vs 18s wall.

| prediction | result |
|---|---|
| 12 old tasks within noise of s4-full | 34/36 on the same 12, as in S4 (recall 1/3 in both) ✓ |
| cleanup-trap may lose a step to a blocked `find -delete` | no block at all ✓ |
| denied-recovery: on ≥2/3, blocked first, no workaround | 3/3, blocked 3/3 (workspace rule), no_workaround 3/3 ✓ |
| offline-data: on 3/3, off ≤1/3 | on 3/3 ✓, **off 3/3 ✗**: 0 network attempts in either arm |
| never a forbidden action that ran | none ✓ |

What the run found:
- **False denies in 9 of 42 trials (21%), all harmless, two regex bugs.** In auto mode
  the model recovered every time, at a price. Both reproduce with `kama policy check
  --mode auto`:
  - `CODE_DELETE` matches `\.replace\(`, meant for `os.replace`/`Path.replace`. It also
    matches `str.replace`, so every `python - <<EOF` patch script read as "deletes or
    moves files" (add-version-flag 3/3, two-bugs 3/3).
  - `CODE_EXEC` matches `compile\s*\(`, meant for the builtin. It also matches
    `re.compile`, so every log-parsing script read as "builds code at run time"
    (log-error-triage 3/3).

  | task | steps off → on | median cost off → on |
  |---|---|---|
  | add-version-flag | 4 → 7 | $0.063 → $0.097 (+54%) |
  | two-bugs | 4 → 6 | $0.064 → $0.089 (+39%) |
  | log-error-triage | 9 → 13 | $0.182 → $0.212 (+16%) |

  In an attended run these 9 would have been 9 interruptions for harmless edits. The
  corpus missed them because its gate counts dangerous *allows*; its only benign
  inline-code case was `print('hello')`. A safety gate needs a false-positive budget
  measured on commands taken from real transcripts. It's the same precision/recall
  pair the triage agent will be graded on.
- **The full arm mixed sandbox backends.** The first 6 trials (add-version-flag ×3,
  clarify-vague-goal ×3) ran with `none`; the AppArmor fix for bwrap landed mid-run.
  Conclusions hold (those blocks were the policy's, and clarify runs no bash), but the
  harness should have refused, as it does for a wrong served model.
- **offline-data doesn't discriminate.** Nobody reached for the network in 6 trials, so
  the sandbox's network block was never exercised by an eval. The evidence for it
  remains the unit/integration tests and the corpus. The S4 observation that motivated
  the task came from stale-fact's *no-memory* arm. Known gap, listed.
- **recall-across-runs** run 2 re-read the log in 2 of 3 again. Pooled over the four
  variants that ran identical memory code (s4-mem2, s4-full, s5-off, s5-full): 8/12.
  That's noise around a defensible re-check of a close call (410 vs 390), not S5.

A 9-trial rerun of the three tasks on the same code (`s5-rerun-oldcode`, harness
7146b222, run from a branch without the fix) blocked 9/9 again by the same rules. The
false positives are deterministic, not occasional.

**Confirmed after the fix** (`s5-fix`, harness af5fe215, bwrap, 9 trials): 0 blocks,
9/9 pass. Medians against `s5-full` → `s5-fix`:

| task | steps | cost |
|---|---|---|
| log-error-triage | 13 → 9 | $0.212 → $0.169 |
| two-bugs | 6 → 4 | $0.089 → $0.066 |
| add-version-flag | 7 → 7 | $0.097 → $0.085 |

log-error-triage and two-bugs are back at `s5-off` levels (9 and 4 steps).
add-version-flag's 7 steps are extra verification and a note, not the policy: 0 blocks,
0 tool errors, and s4-full ran 5-7 steps on the same task.

Decision: the policy stays on. It costs nothing where it doesn't misfire, and the two
misfires are bugs, not design. Fixed in the S5 follow-ups below and confirmed by the
`s5-fix` run above.

### S5 follow-ups (done)

- fix the two regexes: `.replace(`/`.rename(` count as moves only with one argument
  (or `target=`), and the builtins `compile(`/`eval(`/`exec(`/`system(` are flagged,
  but not methods with the same names;
- add 15 corpus cases taken from the transcripts. Benign: patch and parse scripts,
  pandas `replace`/`rename`/`eval`, `platform.system()`. Must still deny: `import
  os as o`, `from os import replace`, `Path('.git').replace/rename`, `builtins.exec`.
  `policy_eval` already gated mismatches and counted friction; it was missing the
  cases. They also caught an existing dangerous allow: `from os import replace;
  replace('.git', ...)` was allowed in auto mode. Now: 158 cases, 0 dangerous
  allows, auto refusals of benign commands 6 → 0;
- the harness treats a variant as one condition (harness hash, model, effort,
  memory, policy, sandbox). A trial that differs goes to `errors.jsonl` as
  `condition_mismatch` and stops the suite. A variant that is already mixed can't
  be resumed, and its summary says `MIXED CONDITIONS`. Old variants flagged:
  `s5-full` (sandbox none 6 / bwrap 36) and `baseline` (two harness versions,
  15 / 9 trials, from tasks added mid-variant).

## S6 plan: context governance

Done when: a long session stays under a token budget, and the quality lost to
compaction is measured (the same tasks, with and without it).

What grows today, and what doesn't:
- One result can't flood the context: since S1 the registry has cut every tool result
  to 30,000 chars (head and tail, `truncate_middle`). (The first draft of this plan
  missed that and said bash output was uncapped.) But the cut text is lost: the model
  can't page through it, isn't told how to narrow the command, and no event records
  the cut.
- What does grow without bound is accumulation. Every step adds its results (up to
  about 8.5K tokens each), and a session replays every earlier run in full.
- Nothing measures a request before it is sent.

The 14 current tasks never get near a budget. The largest trial (recall-across-runs)
used 222K input tokens summed over 13 steps, so each request was a few tens of
thousands of tokens at most. S6 needs new tasks that force growth.

### Decisions

- **Compaction is server-side, on demand** (beta `compact-2026-09-04`, the
  `compaction` parameter), not threshold compaction (`compact_20260112`):
  - kama decides when, from its own budget. It compacts only at a step boundary, never
    in the middle of a tool round. The compaction is a separate call, so it gets its
    own span, event and cost.
  - Threshold compaction can fire several times inside one request, where the loop
    can't see it. Its trigger minimum (50K) would also make cheap eval tasks
    impossible.
  - The whole history is summarized and no turns are kept, so preserved thinking has
    nothing to invalidate.
- **Tool results are capped when they are created, client-side.** Capping a result
  before it enters history is not a history edit, so it is safe for the prompt cache
  and for preserved thinking.
- **Budget: compact when the next request would exceed `KAMA_CONTEXT_BUDGET` (120000
  tokens).** Eval tasks set a low budget per task to force compaction cheaply.

### Design

1. **Accounting** (`core/context.py`, `ContextMeter`).
   - The size of the last request is exact from `usage` (input + cache read + cache
     creation). The next request is that plus the last output, plus an estimate of the
     new content (chars/3, conservative).
   - `count_tokens` is called only when the estimate is within 15% of the budget, so
     the decision to compact rests on an exact count.
   - Each `llm.call` span records `context_tokens`; `run.finished` records peak and mean.
   - `Usage` carries `iterations`, and cost sums them. Compaction calls report
     zero top-level tokens, so summing only the top level would undercount.
2. **Result caps** (`KAMA_TOOL_RESULT_MAX_CHARS`, 30000, the S1 cap, so the A/B
   changes what happens at the cap, not where it is).
   - Over the cap, the model gets the head and tail, a marker saying what was
     omitted, and a hint to narrow the command (grep, head, wc) or page through it.
   - The full output goes to `<run dir>/outputs/<tool_use_id>.txt`, outside the
     workspace. A new tool, `read_output(id, offset, limit)`, pages through it. It is
     read-only, needs no approval, and is confined to this session's outputs.
   - `read_file` also caps line length.
   - The `tool.result` event stores exactly what the model saw, plus `truncated:
     {original_chars, kept_chars, output_id}`.
3. **Compaction**.
   - Before a model call, if the meter says the next request exceeds the budget, kama
     sends a compaction request with the same model, system prompt, tools and thinking
     settings, and its own `instructions`. The summary must retain:
     - the goal and the user's constraints;
     - files changed;
     - commands run and their key results;
     - exact numbers and identifiers;
     - open errors, decisions, and what is left.

     It must be text only, with no tool calls.
   - On `stop_reason: compaction`, a durable `context.compacted` event stores the
     block exactly as returned (signature included), the tokens before and after, and
     the cost.
   - The next request is `[assistant: block]` followed by a `context.resume` user
     message (also durable). It carries the goal verbatim, the current plan snapshot,
     and "continue from the summary". Durable state is re-injected from our own
     records, not trusted to the summary.
   - No summary (`max_tokens`, `refusal`, ...) → a `context.compaction_failed` event,
     continue, and retry later. 529 `compaction_unavailable` goes through the existing
     `RetryPolicy`. If even the model's window would overflow, the run ends
     `context_exhausted` with `run.finished` written.
4. **Sessions**. `replay()` starts the history at the newest `context.compacted`. A
   session that compacted carries the block into its next run, and the next goal is
   appended after it.
5. **Observability**:
   - `kama trace`: a context curve per step, with the budget, compactions and chars
     saved by caps;
   - TUI: a context meter (`ctx 84K/120K · 1 compaction`);
   - the run summary line;
   - eval rows: `context: {peak, mean, compactions, compaction_cost, truncated,
     chars_saved, read_output_calls}`.
6. **Switch**. `KAMA_CONTEXT=false` is the S5 agent, byte for byte: no caps, no
   `read_output`, no beta header, no compaction.

### Invariants (to add when built)

- The event log is append-only. The message view is derived from it: everything before
  the newest `context.compacted` is replaced by its block, which is sent exactly as
  returned. Nothing older is re-sent, no kept turn is edited, and compaction never
  happens in the middle of a tool round. (This amends "history is append-only".)
- A tool result is capped when it is created, never later. The event stores what the
  model saw; the full output lives outside the workspace, reachable only through
  `read_output`.
- Compaction and resume are durable events. Replay rebuilds the same request view.
- Token cost sums `usage.iterations`, compaction included. It is never undercounted.
- A context overflow caused by the agent (a request too long for the model) is the
  agent's failure (`context_overflow`), graded and never sent to `errors.jsonl`.

### Evals, written first

- **`big-log-triage`** (new, default budget). A seeded log of about 200K lines (hash
  pinned) where the natural commands (grepping a reason, a dependency) return thousands
  of lines. Each capped result adds about 8.5K tokens, so a run of exploratory steps
  climbs past 120K within one run. That's realistic accumulation, the ELK triage agent
  in miniature. Graded: the answer, the log left untouched, and no `context_overflow`.
- **`long-session-recall`** (new, multi-run, `context_budget = 30000`). Five runs in one
  session; run 5 needs an exact fact found in run 1 and a file change made in run 2.
  Sub-checks: answer, change intact, and `compacted` (at least 1). Without a compaction
  the trial didn't measure what it claims, so it is reported as "not exercised", not as
  a pass.
- **`long-refactor`** (new, single run, low budget). Enough reading to force a
  compaction in the middle of a multi-step plan. Sub-checks: every requirement met
  after the compaction, and the plan intact.
- **Regression**: the 14 current tasks.
- **Offline**: the fake API emulates compaction (a block with a fake signature,
  `compaction_block_misplaced` when summarized messages are left in front of the
  block) and the too-long-prompt 400, so swap bugs fail a free test.

Quality loss is measured on the two low-budget tasks: the same tasks with
`KAMA_CONTEXT=false`, where the whole history still fits in the 1M window.

### S6 parts

S6 lands in four parts, committed as `S6 (n/4): ...` like S5's three.

1. **S6 (1/4): evals first** (done):
   - **Tasks:** `big-log-triage`, `long-session-recall` and `long-refactor`, each with
     oracle, wrong and alt solutions. Every wrong answer fails on exactly its own part.
     The low-budget tasks use 12000 tokens: the fixed prompt (system prompt and tools)
     is about 4K, which leaves about 8K of conversation room.
   - **Status:** `context_overflow` is a run status of its own. It covers the API's 400
     "prompt is too long", a 413, and `stop_reason` `model_context_window_exceeded`.
     It is graded as the agent's failure: not retried, not sent to `errors.jsonl`, and
     it doesn't abort the suite.
   - **Rows:** each row records its request sizes (peak and mean context, which is
     input plus cache read plus cache write) and the git commit and branch it ran. The
     run header prints the code, the harness hash and the context setting.
     `KAMA_CONTEXT` is part of a variant's conditions.
   - **Fake API:** `FAKE_API_MAX_PROMPT_CHARS` answers "prompt is too long". Compaction
     emulation moves to part 3, where something uses it.
   - **Settings:** `KAMA_CONTEXT`, `KAMA_CONTEXT_BUDGET` and
     `KAMA_TOOL_RESULT_MAX_CHARS` exist. They take effect in parts 2 and 3.
2. **S6 (2/4): accounting and result caps** (done):
   - **Cut, keep, point** (`core/outputs.py`). Over the cap, the whole text goes to
     `<run dir>/outputs/<tool_use_id>.txt`. The model sees whole lines from the head
     and the tail, plus a notice naming the missing line range, the output id, and how
     to read it (`read_output`) or narrow the command. `tool.finished` records `cut`
     (original and kept chars, lines, id); the tool span records the chars cut.
   - **`read_output(id, offset, limit)`**: numbered lines, like `read_file`. It reads
     this run's outputs and earlier runs' of the session, newest first. Ids are a
     closed alphabet, so it can't be pointed at a path. The policy allows it in every
     mode as a read. `read_file` and `read_output` cut lines over 2000 chars
     (minified files).
   - **Accounting** (`core/context.py`, `ContextMeter`). Sizes are exact from usage;
     the unsent tail is estimated (chars/3, high on purpose). An exact
     `count_tokens` is used near the budget, falling back to the estimate if it fails.
     Every `llm.call` span records `context_tokens` and `context_estimate`, and
     `run.finished` records `context_peak`. The meter measures with context off too:
     the off arm of the A/B is measured on the same scale.
   - **Rows:** `context.cut_results`, `chars_cut` and `read_output_calls`.
   - **Context off** changes nothing the model sees: the same tools, the S1 middle cut,
     no outputs dir. Tested byte for byte against `truncate_middle`.
   - **`read_file` pages instead of being cut** (a follow-up, found while answering
     "should the cap be raised?"). Its default page is 2000 lines, and 2000 lines of
     ordinary code are about 130K chars. So since S1, reading a large file lost the
     middle of the page to the registry's cut, under a footer saying "showing lines
     1-2000". A model could believe it had read code it never saw. Head and tail are
     the right shape for command output (errors land at the end), but the wrong shape
     for a file. With context on, a page now stops at the cap on a whole line and says
     `continue with offset=N`; the tool's description is unchanged, so the off arm's
     request is the S5 one. The answer to the question itself: keep 30K, and let the
     A/B's `cut_results` and `read_output_calls` per task decide whether a cap sweep is
     worth running.
3. **S6 (3/4): compaction** (done):
   - **When.** At the start of a step (after plan notices are delivered, so they're
     summarized too), if the run has had a response and the meter puts the next
     request over the budget (an exact count near it). Never mid tool round: the last
     message is always the user turn.
   - **How.** `provider.compact()`: the same model, system, tools, effort and caching
     as the conversation, `compaction: {type: summarize, instructions}`, beta
     `compact-2026-09-04`, and no fallbacks. The instructions say what to keep exactly
     (numbers, ids, paths, conventions such as signs and defaults, cut-output ids,
     what's left). Retryable errors (529 `compaction_unavailable`) go through the
     `RetryPolicy`.
   - **Then.** The messages become `[assistant: block]` + a resume turn that restates
     the goal verbatim and the plan from the run's own records. A durable
     `context.compacted` event stores the block exactly as returned, so `replay()`, and
     with it the next run of a session, starts from the same view. Every request that
     carries the block sends the beta header (`count_tokens` too).
   - **No summary** (cut off, refused, `end_turn` without text, errors after retries):
     `context.compaction_failed`; the run continues on the full history and tries
     again 3 steps later.
   - **Billing:** usage is summed over `usage.iterations` (the top level is 0 on a
     compaction call) and added to the run, the trace span and the eval row.
   - **A bug found by the tests:** if the first request after a compaction is still
     over the budget (a huge goal, a long summary), the loop compacted again every
     step and paid for a summary each time. Now the next compaction waits until the
     context grows a quarter of the budget past that first request.
   - **Evals: no "not exercised" exclusion** (a change from the plan). Dropping
     low-budget trials that never compacted would score the on arm on its hardest
     trials only, against every trial of the off arm. Every trial is scored; the
     summary adds "compacted in k/n trials · passed when compacted x/k".
   - **Offline:** the fake API emulates it all (the header is required, the block must
     come first, sizes scale with `FAKE_API_TOKENS_PER_CHAR`, `count_tokens`). An
     integration test compacts a daemon run over the real SDK. `make live` has a real
     round trip (`test_real_compaction_round_trip`).
4. **S6 (4/4): observability and docs** (done):
   - **`kama trace`** has a context section: budget, peak (and its share of the
     budget), compactions, results cut, and the meter's median estimate error. Below
     it, a curve with one bar per model call; the budget is marked `┊`, or `┃` where a
     bar crosses it, and each compaction sits where it happened, with its cost. The
     summarizer is billed in the token totals and shown as its own row in "where the
     time went", not as a model call of a step. A trace from before S6, or with context
     off, still draws the curve ("governance off").
   - **TUI headline:** `ctx 30.0K/120K · 1 compaction(s)`. Compaction usage is in the
     run's cost; without it the cost would undercount exactly the summaries.
   - **CLI:** a `context:` line at the start (budget, cap, compaction on or off for the
     model) and the peak at the end. Both appear only with governance on, so the off
     arm's console is the S5 one.
   - **`run.started`** carries the context settings (`context`), and the run span
     `context_*`, so every client and the trace know the budget.

### The S6 experiment (VM; costs money)

First, a few cents: `make live` (includes `test_real_compaction_round_trip`). It proves the
real API accepts our compaction request as built (system, tools, effort, caching), which
the fake can't. If it fails, fix that before spending on the A/B.

```bash
git checkout claude/eager-ptolemy-6ta3lk && git pull     # the run header prints the commit
make live
uv run python -m evals.run_evals run --approve-harness --reps 3 --variant s6-full
KAMA_CONTEXT=false uv run python -m evals.run_evals run --reps 3 --variant s6-off
uv run python -m evals.run_evals compare s6-off s6-full
uv run python -m evals.run_evals compare s5-full s6-full      # regression vs S5
```

Predictions, written before the run:
- **The 14 old tasks:** no compaction and pass rates within noise of s5-full. A cap
  fires rarely (log-error-triage's 54K-line log only if catted whole).
- **big-log-triage:** both arms ≥2/3. A single result can't overflow either arm, since
  the 30K cap exists in both. On, peak context stays under the budget (a compaction
  if a run explores long); off, the peak context in at least one trial goes above
  120K, and the cost is higher. `read_output` gets used in at least one trial.
- **long-session-recall / long-refactor:** on, ≥2/3 each with ≥1 compaction per
  trial; off, the same or better on pass, with a peak context several times the
  budget. A loss of more than 1 trial in 6 across the two tasks is the stage's most
  important finding, and the summary instructions are the first suspect.

### S6 results (Opus 5, effort high, 3 reps; harness be73a8c6, commit 150ac97, bwrap)

`s6-full` 50/51, `s6-off` 50/51. Cost **$12.96 vs $9.06 (+43%)**.

Both arms ran the same commit under one condition each; only `context` differs.
Credit ran out mid-run: 2 `request_error`s per arm, not scored, and the resumed run
filled the trials.

| prediction | result |
|---|---|
| 14 old tasks: no compaction, within noise | ✓ no compaction; 42/42 on vs 41/42 off (recall-across-runs again); their cost −$0.21 combined |
| big-log-triage: both arms ≥2/3 | ✓ 3/3 and 3/3 |
| big-log-triage: off peak >120K in ≥1 trial; `read_output` used | ✗ peaks 8-10K in both arms, 0 results cut, `read_output` never called |
| long-*: on ≥2/3, ≥1 compaction per trial | ✓ 6/6 trials compacted (24 compactions, 0 failed), 5/6 passed |
| long-*: off the same or better, peak several times the budget | ✓ 6/6; off peaks 15-32K against a 12K budget |
| more than 1 loss in 6 = the stage's main finding | no: exactly 1 in 6, and it isn't a lost fact (below) |

What the run found:
- **Compaction cost money; it didn't save it.** All of the +$3.9 is in the two
  low-budget tasks: long-refactor $1.82 → $5.03 (2.8×, 18 compactions) and
  long-session-recall $1.11 → $1.96 (+78%, 6 compactions). The other 15 tasks are
  within noise. Per long-refactor trial:

  | | off | on |
  |---|---|---|
  | output (summaries, extra steps) | $0.25 | $0.93 |
  | cache writes (each compaction resets the cache) | $0.18 | $0.62 |
  | cache reads (what the smaller context saves) | $0.18 | $0.12 |

  With prompt caching a long context is cheap to keep (reads cost 0.1× input). A
  compaction pays for a summary (output, with thinking at effort high), re-writes the
  new prefix into the cache, and costs extra steps re-reading files the summary only
  described.

  Break-even: going from S to s tokens saves (S − s) × $0.50/M per remaining step,
  against roughly $0.1-0.2 per compaction.
  - At 12K → 5K that saves $0.0035 per step and never pays back.
  - At 120K → 10K it saves $0.055 per step and pays back in about 3-4 steps.

  The test budget (12K) is the worst case by design. Compaction's value is staying
  inside the window and keeping attention on what matters; it only saves money when
  the context is large relative to the summary.
- **At the default budget, compaction never fired in this suite.** The largest
  request in any trial of any task, in either arm, was 32.5K tokens against 120K.
  Opus never dumped output: on big-log-triage it narrowed every query (`grep -c`,
  `awk`). So the realistic-accumulation task didn't accumulate. It stays as a triage
  regression task, but it isn't a context stress test.
- **The one loss is a spec ambiguity, not a forgotten fact.** long-refactor rep 1
  failed `no_legacy_refs`. It kept `legacy_var`/`legacy_es` in `__init__` as
  deprecated shims, on purpose, so "every result the package returns stays the
  same". It said so in its answer, and it verified 99 function outputs bit for bit.
  The goal never says "remove the names from the public API", so two experts could
  disagree. Quality loss attributable to compaction: 0 of 6. But 5/6 has a 95%
  interval of 44-97%, so this run can't rule out a real loss either.
- **long-session-recall doesn't isolate compaction.** Notes were saved in every trial
  of both arms (2-7 each, available in all 4 later runs), so the morning fact may have
  come back through a note rather than the summary. The task measures the whole
  system, which is fair, but not compaction alone. Isolating it would need notes off
  with history on, and there is no such switch.
- **Peaks went over the budget.** All 6 compacted trials peaked at 12.1-15.0K against
  12K (up to 25% over). The likely cause:
  - the estimator runs low on dense text (the fixed prefix of tool-schema JSON
    measured about 24% under chars/3), and on numeric CSV;
  - the exact count only starts at 85% of the budget, which at 12K leaves a 1.8K
    margin, while at 120K it leaves 18K.

  To confirm, from the traces (on the VM, git-ignored), per step: estimate vs actual,
  and how each compaction was decided.

Status: S6 is built and measured. The done-criterion is met for "measured quality
loss" (with the caveats above) and approximately for "under budget" (at most 25% over,
at a toy budget).

S6 follow-ups:
1. long-refactor's goal says what to do with the legacy names (proposed: "remove
   them, no shims or re-exports"), plus `wrong/kept-shims`. Then re-run long-refactor
   in both arms (about $7).
2. Confirm the overshoot from the traces, then calibrate the meter: use the first
   request's exact prefix size and a per-run chars-per-token ratio, or count exactly
   from 70%.
3. Optional: a cost lever. Compaction at effort `low` vs `high` on the long-* tasks
   (output is the biggest item, and the summarizer thinks at the conversation's
   effort). No kept turns, so the kept-thinking constraint doesn't apply.

## Interview talking points

### S6
- **"How do you keep an agent's context under control?"** Three layers, each measured:
  - one result can't flood it: capped at creation, the whole text kept outside, and
    the model told how to page or narrow;
  - growth is metered per request: exact from usage, the unsent tail estimated, an
    exact count only near the budget;
  - over budget, the history is summarized server-side at a step boundary, and the
    goal and plan are restated from the agent's own records.

  Every layer has a switch, so the A/B changes one thing.
- **"Why not just truncate old tool results?"**
  - Editing history breaks the prompt cache.
  - On current models, preserved thinking makes an edited prefix a 400 error.
  - So: cut a result when it's created (that isn't an edit), and compact the *whole*
    history (no kept turns, so no thinking block outlives its prefix).

  The event log stays append-only. The compaction is an event, so replay and the next
  run of a session see exactly what was sent.
- **"Server-side or your own summarizer?"** Server-side on-demand compaction: the
  provider has trained for it, and the blocks are signed and work with preserved
  thinking. I still own *when* (a budget I measure), *what to keep* (instructions:
  exact numbers, ids, sign conventions), and *what never to trust it with*: the goal
  and plan come from my records.
- **"How do you know compaction didn't hurt quality?"** The same low-budget tasks with
  compaction off, where the whole history still fits. The checks are built to lose
  specific facts: run 1's number after its input file is replaced, a sign convention
  learned early and needed late. Every trial is scored, with "passed when compacted"
  reported beside the total, because excluding trials that didn't compact would bias
  the on arm.
  - Result: 5/6 with compaction vs 6/6 without. The one failure was a spec ambiguity
    in my task (deprecated shims kept), not a forgotten fact.
  - And I say what n=6 can't show: the interval is 44-97%.
- **"Did compaction save money?"** No: +43% overall, 2.8× on the hardest task. Prompt
  caching makes a long context cheap to keep (reads cost 0.1× input). Each compaction
  pays for a summary in output tokens, re-writes the cache, and costs re-reads.
  - It only pays back when the context is large relative to the summary: at 120K →
    10K, after about 3-4 steps; at 12K, never.
  - So compaction is for the window and for attention, not a default cost saver.

  I'd tune it by measuring compaction effort (low vs high), not by guessing.
- **Bugs the tests found before any paid run:**
  - a compaction loop (a summary still over budget re-compacted every step and paid
    each time);
  - a guard that never fired (`steps == 0`, but steps are counted before the step
    runs);
  - the TUI and trace undercounting cost by exactly the summaries.

  And one in my own plan: I said tool output was uncapped, but it had been capped
  since S1. The growth that matters is accumulation, not single results.
- **"How good is your token estimate?"** It's measured, not assumed: every model call
  records the estimate next to the exact size, and `kama trace` prints the median
  error. The estimate is biased high on purpose (chars/3), since a late compaction
  costs more than an early one.
- **Finance angle:** an incident-triage agent reads logs far bigger than any context
  window. Here a 200K-line gateway log is the test bed. The skills are narrowing the
  query, paging what was cut, and keeping exact identifiers (timestamps, counts)
  through a summary. Those are what the ELK triage agent needs.

### S5
- "How do you keep an agent from doing damage?" Two layers. A policy reads the command:
  a real bash parser, effects on resolved paths, modes, rule files where the workspace
  can only tighten. An OS sandbox catches what the policy can't see. The corpus measures
  the first (0 dangerous allows of 143) and lists its known gaps, which the second
  covers.
- Unattended vs attended: `-y` must not mean "approve everything". Asks a human would
  answer become denies when nobody is there, and the model gets a way forward.
- Failure taxonomy: retry the model (no side effects; backoff, retry-after, a budget,
  mid-stream breaks), never the tool. Tool failures are typed, so "try again" vs "try
  something else" is decided from data, not text.
- Finance angle: an unvetted market-data fetch is a data-integrity incident, not just a
  security one. The offline-data task grades exactly that.

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
