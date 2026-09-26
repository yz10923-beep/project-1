# Evals: what you need to know, applied to kama

An eval answers one question: **did this change make the agent better or worse, and by
how much?** Without one, "the new prompt feels better" is all you have. With one, you
have a number, an error bar and a transcript for every case.

## 1. Vocabulary

| Term | Meaning | In this repo |
|---|---|---|
| **Task** (case) | One input plus a definition of success | `evals/tasks/<id>/` |
| **Trial** | One attempt at a task. Models are non-deterministic, so you run several | one `(task, rep)` |
| **Grader** (checker) | Code (or a model, or a human) that scores a trial | `evals/tasks/<id>/check.py` |
| **Transcript** (trajectory) | Everything the agent did: messages, tool calls, results | `traces/<id>_rep<k>.json` |
| **Outcome** | The state of the world *after* the trial, not what the agent claims | the temp workspace on disk |
| **Eval harness** | Runs trials, isolates them, grades, records | `evals/harness.py` |
| **Agent harness** (scaffold) | What makes the model an agent | `src/kama_claude/core/agent/` |

An eval has three parts: **inputs**, **a way to run the real app on each input**, and
**a way to grade each output**. Everything else is making those three trustworthy.

## 2. The most important rule for agents: grade the outcome, not the transcript

An agent's summary is a claim. The files on disk are the fact. Your own VWAP run
reported "12 passed", but it wrote the tests it was graded on. So every `check.py` here
inspects the workspace after the run, using data the agent never saw:

- `fix-add-bug` calls `add()` on hidden inputs, and fails the trial if `test_calc.py` was
  edited (making the test pass by changing the test).
- `vwap-cli` generates a *different* trade file, recomputes VWAP independently, and
  compares every row.
- `clarify-vague-goal` passes only if **nothing** changed and the reply asks a question.

Graders check **what** was produced, never **how**. Don't require a particular sequence of
tool calls; agents find valid paths you didn't anticipate. Use the transcript for
process guardrails only (did it read the answer key, did it ask before deleting).

## 3. Choosing a grader, cheapest first

1. **Code-based**: tests pass, file exists, numbers match, nothing forbidden touched.
   Deterministic, free, debuggable. Default for anything with a checkable end state.
2. **LLM judge**: for subjective qualities such as a readable diff or a clear explanation.
   It needs a concrete rubric ("cites at least one log line", not "rate 1-5"),
   structured output, a judge model different from the one under test, and
   **calibration**: grade about 30 cases by hand and confirm the judge agrees on nearly all
   the clear-cut ones. Watch for position bias and verbosity bias.
3. **Human**: the gold standard, and slow. Use it to calibrate 1 and 2.

## 4. Writing a good task

- **Two experts would agree on pass or fail.** If not, the spec is ambiguous; fix the
  goal (see how `vwap-cli` pins the output format exactly).
- **Give the symptom, not the diagnosis.** "The tests are failing" is what a user says.
  "Fix the minus sign on line 2 of calc.py" measures whether the model can read a hint.
- **Hidden checks.** Test on inputs the agent can't see, or it can pass by hardcoding
  (`wrong/hardcode` in `fix-add-bug` is that cheat, and it fails).
- **A reference solution that passes** (`oracle/`) proves the task is solvable and the
  grader works.
- **Known-bad solutions that fail** (`wrong/<name>/`). Each is a plausible mistake or a
  cheat. If one passes, the grader is too lenient.
- **Differently formatted correct answers that pass** (`alt/<name>/`). If one fails, the
  grader is too rigid and is scoring formatting, not correctness.
- **Test both directions.** If every task rewards acting, "always act" scores 100%.
  `clarify-vague-goal` rewards *not* acting. Add refusal-worthy and ask-first cases.
- **Take tasks from real use.** Your own runs, failures and complaints are the best source.
  `clarify-vague-goal` is your actual first run. `vwap-cli` is your second.

`selftest` runs every oracle, alt, null (did nothing) and wrong solution through the
checkers in about two seconds, for free. It runs inside `make verify` and before every
paid run.

### Reference task: `log-error-triage`

Copy this one when you write a task that is hard for the right reasons. It asks for the
service with the most ERRORs in a one-hour window, the count, and the earliest message,
over a 54k-line (5.4 MB) trading-platform log. Too big to read through the tools, so
the agent has to filter it.

| File | Role |
|---|---|
| `task.toml` | Goal written as an on-call request, with an exact output spec (inclusive/exclusive window, JSON shape) so pass/fail is unambiguous |
| `setup.py` | Generates the log from a fixed seed at trial start, because it's too big to commit. Each trap is **built explicitly**, not left to chance |
| `check.py` | Recomputes the answer from `setup.truth()`, never from the agent's copy of the log. Fails if the log was altered. Lenient on format, strict on content. The reason lists each part |
| `make_answers.py` | Derives `oracle/`, `wrong/` and `alt/` by running each naive approach on the actual file, and asserts every trap still gives a different answer |
| `wrong/*` | whole-file count, inclusive end, exclusive start, case-insensitive `grep error`, first in file instead of first by time, tampered log |
| `alt/loose-formatting` | upper-case service, count as a string, quoted message: must pass |

Each trap is a real on-call mistake. A test pins the log's SHA-256, so any change to the
generator (or to Python's `random`) shows up as a failing test instead of silently
changing what earlier scores meant.

## 5. Metrics and error bars

- **Pass rate** = passed trials / scored trials, reported with a **95% Wilson interval**.
- **pass@k** = share of tasks where *at least one* of k trials passed. That's capability:
  can it ever do this?
- **pass^k** = share of tasks where *all* k trials passed. That's reliability: can you
  trust it? For a product, especially in finance, pass^k is the one that matters.
- **Noise floor ≈ ±1/√(tasks × reps).** Differences smaller than this aren't real.

  | tasks × reps | trials | noise floor |
  |---|---|---|
  | 8 × 3 | 24 | ±20 pts |
  | 20 × 3 | 60 | ±13 pts |
  | 40 × 3 | 120 | ±9 pts |

  So 8 tasks can catch a big regression but not a 5-point improvement. Grow the set
  as you go.
- **Side metrics** go in their own columns, never blended into the score: steps, tool
  errors, tokens by type, cost, LLM time, wall time. Report **absolute numbers first**
  ("$0.21 and 7 steps per trial"), then relative change.
- **Cost** is derived from the recorded tokens and the model that actually served the call:
  `(input·p_in + cache_write·1.25·p_in + cache_read·0.1·p_in + output·p_out) / 1e6`.
  Failed attempts cost money too, and the summary shows them separately.

## 6. Hygiene: what the harness already enforces, and why

Each of these prevents a specific way to get a confident but wrong number:

| Property | Where | What it prevents |
|---|---|---|
| Fresh temp workspace per trial | `fresh_workspace` | one trial's files leaking into the next |
| Run logs stored outside the workspace | `runs_dir` override | the agent reading earlier runs (it did this in your run 2) |
| API errors, timeouts and grader crashes go to `errors.jsonl` | `run_trial` | plumbing failures scored as model failures |
| Truncated runs are shown but not averaged | `status: truncated` | a cut-off answer counted as a wrong one |
| Hard wall-clock limit per trial | `--timeout-s` | one hung trial stalling the suite |
| Serving errors retried with jittered backoff; attempts recorded | `run_trial` | a transient 529 counted as a failure |
| Served model asserted; refusal fallback off | `model_mismatch`, `run_evals.py` | scoring a different model than the one you asked for |
| Harness hash must be approved by you | `--approve-harness` | scores silently changing because a grader got easier |
| Answer-key access flagged | `leak_suspect` | the agent `cat`-ing `check.py` |
| Resume per (task, rep) | `done_keys` | a crash costing the trials that already finished |

**Known gap, and an honest interview answer:** `bash` isn't sandboxed, so an agent
*could* read `evals/tasks/*/check.py`. The harness detects this but can't prevent it.
The structural fix is to run each trial in a container that has no copy of the eval
directory. It's a good S5 follow-up.

## 7. Workflow

```bash
uv run python -m evals.run_evals list        # 1. read every task: are these the cases you care about?
uv run python -m evals.run_evals selftest    # 2. are the graders sane? (free)
uv run python -m evals.run_evals run --reps 1 --tasks fix-add-bug --approve-harness
                                             # 3. pilot one trial; you approve the harness here
# 4. READ the trace: evals/results/kama-run/baseline/traces/fix-add-bug_rep0.json
#    Does the grade match what you would have given?
uv run python -m evals.run_evals run --reps 3   # 5. full baseline (resumes, never repeats)
# 6. change one thing (prompt, tool, model), then:
uv run python -m evals.run_evals run --reps 3 --variant v1
uv run python -m evals.run_evals summary --variant v1
```

**Read transcripts.** A surprising score is more often a grader bug than a model fact.
Look at every failure until you trust the grader, and after that at a sample.

Cost: your two real runs cost about $0.11 and $0.29. Eight tasks × 3 reps is 24 trials,
so expect a few dollars. Measure it with the pilot rather than guessing.

## 8. Capability vs regression

- **Capability evals** should start with a *low* pass rate. They are the targets.
- **Regression evals** should stay near 100%. They catch backsliding.

When a capability task has passed every time for a while, it becomes a regression task.
When every task passes, the eval is saturated and can't show improvement, so add harder
ones.

## 9. How this carries over to the incident-triage agent

The same harness shape, with different graders:

| Triage metric | Grader |
|---|---|
| Classification (macro-F1) | code: predicted label vs your labelled incident. Macro-F1 over classes, so rare incident types count as much as common ones |
| Evidence grounding | code first: every log line the agent cites must exist in the ELK index for that incident's time window. A judge only for "does this evidence support the conclusion" |
| Trajectory efficiency | from events: queries/steps/tokens vs a reference trajectory, reported alongside, never blended |
| Both directions | include "no incident / noise" cases, so "always raise an alarm" can't score well |

The ELK index is the "fixture". You need a *frozen* snapshot per incident (a test index or
exported documents), or the eval changes whenever the logs do.

## 10. What an interviewer will probe

- "How do you know your agent works?" → end-state graders on hidden checks, pass rate
  with an interval, pass^k for reliability, transcripts for every trial.
- "How do you know your grader works?" → oracle / null / wrong-solution self-test,
  manual review of the pilot's transcripts, a hash that locks the grader.
- "How big a change can you detect?" → noise floor ±1/√(n·R), and what you did about it.
- "What went wrong?" → your agent read its own logs to recover memory, so logs now live
  outside the workspace, and answer-key access is flagged.

## Reading list

- Anthropic, [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents):
  the vocabulary above, grader types, pass@k vs pass^k, "start with 20-50 tasks drawn
  from real failures". Read this first.
- Anthropic, [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents):
  uses evals to improve tool design. Relevant to S5.
- [SWE-bench Verified](https://www.emergentmind.com/topics/swe-bench-verified-issues):
  the standard coding-agent benchmark. Hidden tests, fail→pass, pass@k. Your tasks are a
  small version of this.
- [SWE-bench Verified is flawed despite expert review](https://medium.com/@danieldkang/swe-bench-verified-is-flawed-despite-expert-review-utboost-exposes-gaps-in-test-coverage-4b75c6b940c6):
  weak hidden tests let wrong patches pass. This is why `wrong/` solutions exist.
