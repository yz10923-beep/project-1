"""Per model call: what the context meter predicted vs the request's exact size, and how
each compaction was decided (S6). Reads an eval variant's traces (git-ignored, so run
it where the eval ran) or any directory of runs.

    uv run python scripts/context_curves.py s6-full [--tasks long-]
    uv run python scripts/context_curves.py ~/.kama/runs

Negative error = the meter underestimated. Below the exact-count line (85% of the
budget) the estimate alone decides, so an underestimate there can send a request over
the budget.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "evals" / "results" / "kama-run"


def spans(path: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


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
        budget = (run or {}).get("attrs", {}).get("context_budget")  # type: ignore[union-attr]
        for s in sorted(rows, key=lambda s: int(s["start_ns"])):  # type: ignore[call-overload]
            a: dict[str, object] = s["attrs"]  # type: ignore[assignment]
            if s["name"] == "llm.call" and "context_tokens" in a:
                est, act = a.get("context_estimate"), int(a["context_tokens"])  # type: ignore[call-overload]
                err = (int(est) - act) / act if est and act else None  # type: ignore[call-overload]
                if err is not None:
                    errors.append(err)
                over = " OVER BUDGET" if budget and act > int(budget) else ""  # type: ignore[call-overload]
                below = (
                    " (estimate below the exact-count line)"
                    if budget and est and int(est) < 0.85 * int(budget)  # type: ignore[call-overload]
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
                )
            elif s["name"] == "llm.call":
                print(f"  step {a.get('step')!s:>3}  (no context_tokens on this llm.call span)")
    if errors:
        under = sum(e < 0 for e in errors)
        print(
            f"\n{len(errors)} calls · estimate error median {statistics.median(errors):+.0%}, "
            f"min {min(errors):+.0%}, max {max(errors):+.0%} · underestimates {under}"
        )


if __name__ == "__main__":
    main()
