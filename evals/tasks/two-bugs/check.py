"""Pass = both bugs fixed (checked on fills the visible tests never use) and the test
file untouched. The reason names which bug remains, so "stopped after the first fix"
is visible at a glance."""

import json
from pathlib import Path

from evals.harness import Outcome, run_py

PROBE = """
import json
out = {}
try:
    from portfolio import Fill, avg_cost, net_position, unrealized_pnl
    out["imports"] = True
except Exception as e:
    out["imports"] = False
    out["import_error"] = f"{type(e).__name__}: {e}"
else:
    fills = [
        Fill("XOM", "BUY", 10, 100.0),
        Fill("XOM", "BUY", 90, 110.0),
        Fill("XOM", "SELL", 30, 120.0),
        Fill("XOM", "SELL", 20, 90.0),
        Fill("CVX", "BUY", 5, 150.0),
    ]
    out["net_position"] = net_position(fills, "XOM") == 50 and net_position(fills, "CVX") == 5
    out["avg_cost"] = abs(avg_cost(fills, "XOM") - 109.0) < 1e-9 and avg_cost(fills, "BP") == 0.0
    out["unrealized_pnl"] = abs(unrealized_pnl(fills, "XOM", 112.0) - 150.0) < 1e-9
print(json.dumps(out))
"""


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    if "test_portfolio.py" in outcome.changed:
        return False, "edited the tests instead of the code"
    r = run_py(ws, "-c", PROBE)
    if r.returncode != 0:
        return False, f"probe crashed: {r.stderr.strip()[-200:]}"
    got = json.loads(r.stdout)
    if not got["imports"]:
        return False, f"bug 1 (import) not fixed: {got['import_error']}"
    parts = {k: got[k] for k in ("net_position", "avg_cost", "unrealized_pnl")}
    detail = "import fixed, " + ", ".join(f"{k} {'ok' if v else 'WRONG'}" for k, v in parts.items())
    return all(parts.values()), detail
