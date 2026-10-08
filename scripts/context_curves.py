"""Per model call: what the context meter predicted vs the request's exact size, and how
each compaction was decided (S6). Reads an eval variant's traces (git-ignored, so run
it where the eval ran) or any directory of runs.

    uv run python scripts/context_curves.py s6-full [--tasks long-]
    uv run python scripts/context_curves.py ~/.kama/runs

Each trace ends with where its time went: model calls vs compactions (a row's
latency_s counts model calls only). Negative error = the meter underestimated. Below
the exact-count line (85% of the budget) the estimate alone decides, so an
underestimate there can send a request over the budget.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parents[1] / "evals" / "results" / "kama-run"


def spans(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _secs(span: dict[str, Any]) -> float:
    return int(span["duration_ns"]) / 1e9


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("where", help="an eval variant (e.g. s6-full) or a directory of runs")
    ap.add_argument("--tasks", default="", help="only trace paths containing this text")
    args = ap.parse_args()
    root = Path(args.where).expanduser()
    if not root.is_dir():
        root = RESULTS / args.where / "events"
    paths = sorted(p for p in root.rglob("trace.jsonl") if args.tasks in str(p))
    if not paths:
        sys.exit(f"no trace.jsonl under {root}")
    errors: list[float] = []
    for path in paths:
        print(path.relative_to(root))
        rows = spans(path)
        run = next((s for s in rows if s["name"] == "run"), None)
        budget = (run or {}).get("attrs", {}).get("context_budget")
        for s in sorted(rows, key=lambda s: int(s["start_ns"])):
            a: dict[str, Any] = s["attrs"]
            if s["name"] == "llm.call" and "context_tokens" in a:
                est, act = a.get("context_estimate"), int(a["context_tokens"])
                err = (int(est) - act) / act if est and act else None
                if err is not None:
                    errors.append(err)
                over = " OVER BUDGET" if budget and act > int(budget) else ""
                below = (
                    " (estimate below the exact-count line)"
                    if budget and est and int(est) < 0.85 * int(budget)
                    else ""
                )
                print(
                    f"  step {a.get('step')!s:>3}  estimate {est!s:>7}  actual {act:>7}  "
                    f"err {'?' if err is None else f'{err:+.0%}':>5}{over}{below if over else ''}"
                )
            elif s["name"] == "context.compact":
                print(
                    f"  step {a.get('step')!s:>3}  COMPACT  tokens_before {a.get('tokens_before')}"
                    f"  measured {a.get('measured')}  {s['status']}"
                    f"  {_secs(s):.0f}s  {a.get('output_tokens', '?')} out"
                )
            elif s["name"] == "llm.call":
                print(f"  step {a.get('step')!s:>3}  (no context_tokens on this llm.call span)")
        # where the wall time went: compaction is not in a row's latency_s (LLM time)
        compact = [_secs(s) for s in rows if s["name"] == "context.compact"]
        calls = sum(_secs(s) for s in rows if s["name"] == "llm.call")
        wall = _secs(run) if run else 0.0
        print(
            f"  time: run {wall:.0f}s · model calls {calls:.0f}s · "
            f"compaction {sum(compact):.0f}s in {len(compact)}"
            + (f" (median {statistics.median(compact):.0f}s)" if compact else "")
        )
    if errors:
        under = sum(e < 0 for e in errors)
        print(
            f"\n{len(errors)} calls · estimate error median {statistics.median(errors):+.0%}, "
            f"min {min(errors):+.0%}, max {max(errors):+.0%} · underestimates {under}"
        )


if __name__ == "__main__":
    main()
