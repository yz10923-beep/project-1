"""Eval CLI.

uv run python -m evals.run_evals list                 # read every task (sign-off)
uv run python -m evals.run_evals selftest             # graders vs oracle/null/wrong; free
uv run python -m evals.run_evals run --reps 3         # real agent, real API: costs money
uv run python -m evals.run_evals summary              # re-print the summary
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from evals.harness import (
    APPROVAL_FILE,
    RESULTS_DIR,
    RunConfig,
    SuiteAborted,
    harness_sha,
    load_tasks,
    run_suite,
    selftest,
    summarize,
)
from kama_claude.core.config import load_settings
from kama_claude.core.llm.anthropic_provider import supports_effort


def _check_approval(approve: bool) -> None:
    """Scores are only comparable if the harness and graders did not change between runs.
    Any edit to them needs a human to re-approve; an agent tuning the prompt must never
    be able to quietly make the grader easier."""
    sha = harness_sha()
    if approve:
        APPROVAL_FILE.write_text(sha + "\n")
        print(f"harness approved: {sha[:12]}")
        return
    approved = APPROVAL_FILE.read_text().strip() if APPROVAL_FILE.exists() else ""
    if approved != sha:
        print(
            "harness or tasks changed since last approval "
            f"(approved={approved[:12] or 'none'}, now={sha[:12]}).\n"
            "Review `git diff evals/`, run `selftest`, then re-run with --approve-harness.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def main() -> None:
    ap = argparse.ArgumentParser(prog="run_evals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("selftest")
    run = sub.add_parser("run")
    run.add_argument("--tasks", help="comma-separated task ids (default: all)")
    run.add_argument("--reps", type=int, default=3)
    run.add_argument("--variant", default="baseline", help="baseline, v1, v2, ...")
    run.add_argument("--model", help="override KAMA_MODEL")
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--timeout-s", type=float, default=900)
    run.add_argument("--approve-harness", action="store_true")
    summ = sub.add_parser("summary")
    summ.add_argument("--variant", default="baseline")
    args = ap.parse_args()

    if args.cmd == "list":
        for t in load_tasks():
            print(f"### {t.id}  [{', '.join(t.tags)}]\n{t.goal}\n")
        return
    if args.cmd == "selftest":
        tasks = load_tasks()
        problems = selftest(tasks)
        for p in problems:
            print("PROBLEM:", p)
        print(f"selftest: {len(tasks)} tasks, {len(problems)} problems")
        raise SystemExit(1 if problems else 0)
    if args.cmd == "summary":
        print(summarize(RESULTS_DIR / args.variant))
        return

    tasks = load_tasks(args.tasks.split(",") if args.tasks else None)
    if problems := selftest(tasks):
        raise SystemExit("selftest failed; fix graders first:\n" + "\n".join(problems))
    _check_approval(args.approve_harness)
    settings = load_settings()
    # Refusal fallback would let a different model answer some trials; for a clean
    # comparison every trial must be served by the model under test.
    update: dict[str, object] = {"refusal_fallback": False}
    if args.model:
        update["model"] = args.model
    settings = settings.model_copy(update=update)
    if settings.anthropic_api_key is None:
        raise SystemExit("no ANTHROPIC_API_KEY (env, ./.env or ~/.kama/.env)")
    effort = settings.effort if supports_effort(settings.model) else None
    print(
        f"running {len(tasks)} tasks × {args.reps} reps on {settings.model} "
        f"(effort={effort or 'API default'}) "
        f"(variant={args.variant}, concurrency={args.concurrency}); the agent's bash is "
        "auto-approved, so run this on a disposable machine"
    )
    cfg = RunConfig(
        settings=settings,
        variant=args.variant,
        reps=args.reps,
        concurrency=args.concurrency,
        timeout_s=args.timeout_s,
    )
    try:
        asyncio.run(run_suite(tasks, cfg))
    except SuiteAborted as e:
        raise SystemExit(
            f"\nABORTED: a request error will repeat on every trial, so the run stopped.\n{e}\n"
            "Fix the configuration, then re-run the same command (scored trials are kept)."
        ) from e
    print()
    print(summarize(cfg.variant_dir))


if __name__ == "__main__":
    main()
