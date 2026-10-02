"""Grades each of the ten SPEC.md requirements separately, on data the agent never sees.
Pass = all ten. The reason names the failed ones (e.g. "8/10 · failed R9 R10"), which
is exactly what a stopped-early run looks like.

Expected values are worked out by hand in the comments below, not by a second
implementation, so the oracle and the checker cannot share a bug."""

import ast
import json
import tempfile
from pathlib import Path

from evals.harness import Outcome, run_py

# 12 rows, 4 rejected (qty 0, side HOLD, qty "abc", qty -5); out of time order; messy
# symbols and sides.
TRADES = """ts,symbol,side,qty,price
2024-03-15T09:35:00,msft,BUY,10,400.00
2024-03-15T09:30:00, aapl ,buy,100,170.00
2024-03-15T09:40:00,AAPL,BUY,50,176.00
2024-03-15T09:45:00,AAPL,SELL,120,180.00
2024-03-15T09:50:00,MSFT,Sell,4,410.00
2024-03-15T09:55:00,JPM,BUY,0,190.00
2024-03-15T10:00:00,JPM,HOLD,5,190.00
2024-03-15T10:05:00,JPM,BUY,abc,190.00
2024-03-15T10:10:00,XOM,BUY,30,110.00
2024-03-15T10:15:00,XOM,SELL,30,115.00
2024-03-15T10:20:00,NVDA,BUY,-5,900.00
2024-03-15T10:30:00,JPM,BUY,20,195.50
"""
PRICES = "symbol,price\naapl,181.00\nMSFT , 415.50\nJPM,200.00\nXOM,116.00\n"
PRICES_NO_JPM = "symbol,price\nAAPL,181.00\nMSFT,415.50\nXOM,116.00\n"

# positions: AAPL 100+50-120=30, MSFT 10-4=6, JPM 20, XOM 30-30=0 (left out)
POSITIONS = {"AAPL": 30, "MSFT": 6, "JPM": 20}
# avg cost AAPL (100*170 + 50*176) / 150 = 172.0; an average-cost P&L would be 960
AVG = {"AAPL": 172.0, "MSFT": 400.0, "XOM": 110.0, "ZZZ": 0.0}
# FIFO AAPL: sell 120 = 100 @170 (+1000) + 20 @176 (+80); MSFT 4*10; XOM 30*5; JPM 0
PNL = {"AAPL": 1080.0, "MSFT": 40.0, "XOM": 150.0, "JPM": 0.0}
# market value AAPL 30*181, MSFT 6*415.5, JPM 20*200
EXPOSURE = {"AAPL": 5430.0, "MSFT": 2493.0, "JPM": 4000.0}
REPORT = {
    "positions": POSITIONS,
    "gross_exposure": 11923.0,
    "realized_pnl": 1270.0,
    "top": ["AAPL", "JPM", "MSFT"],
    "rejected_rows": 4,
}

PROBE = r"""
import json, sys
from datetime import datetime
out = {}
def safe(key, fn):
    try:
        out[key] = fn()
    except Exception as e:
        out[key] = {"error": f"{type(e).__name__}: {e}"}
from riskreport.model import Trade
from riskreport import io, metrics
trades_path, prices_path = sys.argv[1], sys.argv[2]

def r1():
    trades, rejected = io.load_trades(trades_path)
    ts = [t.ts for t in trades]
    return {
        "n": len(trades), "rejected": rejected,
        "sorted": ts == sorted(ts), "datetimes": all(isinstance(x, datetime) for x in ts),
        "first": [trades[0].symbol, trades[0].side, trades[0].qty, trades[0].price],
        "types": all(type(t.qty) is int and type(t.price) is float for t in trades),
        "symbols": sorted({t.symbol for t in trades}), "sides": sorted({t.side for t in trades}),
    }
safe("R1", r1)
safe("R3", lambda: io.load_prices(prices_path))
trades = io.load_trades(trades_path)[0] if isinstance(out["R1"], dict) and "n" in out["R1"] else []
safe("R4", lambda: metrics.positions(trades))
safe("R5", lambda: {s: metrics.avg_cost(trades, s) for s in ["AAPL", "MSFT", "XOM", "ZZZ"]})
safe("R6", lambda: {s: metrics.realized_pnl(trades, s) for s in ["AAPL", "MSFT", "XOM", "JPM"]})

def r6b():
    # partial lots across two sells: 5*(120-100) + 5*(90-100) + 5*(90-110) = -50
    t = lambda m, side, q, p: Trade(datetime(2024, 1, 2, 10, m), "Q", side, q, p)
    return metrics.realized_pnl(
        [t(0, "BUY", 10, 100.0), t(1, "BUY", 10, 110.0), t(2, "SELL", 5, 120.0),
         t(3, "SELL", 10, 90.0)], "Q")
safe("R6b", r6b)
safe("R7", lambda: metrics.exposure({"AAPL": 30, "MSFT": 6, "JPM": 20},
                                      {"AAPL": 181.0, "MSFT": 415.5, "JPM": 200.0}))
def r7b():
    try:
        metrics.exposure({"GS": 1}, {"AAPL": 1.0})
    except KeyError as e:
        return "GS" in str(e)
    return False
safe("R7b", r7b)
print(json.dumps(out))
"""

REQUIREMENTS = [f"R{i}" for i in range(1, 11)]


def _close(a: object, b: float) -> bool:
    return isinstance(a, (int, float)) and abs(a - b) < 0.006


def _close_map(got: object, want: dict[str, float]) -> bool:
    return (
        isinstance(got, dict)
        and got.keys() == want.keys()
        and all(_close(got[k], v) for k, v in want.items())
    )


def _cli(ws: Path, *args: str) -> tuple[int, dict | None, str]:
    r = run_py(ws, "-m", "riskreport", *args)
    try:
        report = json.loads(r.stdout)
    except ValueError:
        report = None
    return r.returncode, report if isinstance(report, dict) else None, r.stderr


def _report_errors(report: dict | None, top: list[str]) -> list[str]:
    """Which report fields are wrong, as "field got != want", so a systematic mistake
    (e.g. realized P&L summed over open positions only) reads straight off the reason."""
    if report is None:
        return ["stdout is not one JSON object"]
    want = {**REPORT, "top": top}
    errors = []
    for key in ("positions", "gross_exposure", "realized_pnl", "top", "rejected_rows"):
        got = report.get(key)
        same = (
            _close(got, want[key])
            if key in ("gross_exposure", "realized_pnl")
            else got == want[key]
        )
        if not same:
            errors.append(f"{key} {got!r} != {want[key]!r}")
    return errors


def _tests_ok(ws: Path, outcome: Outcome) -> bool:
    protected = [p for p in outcome.changed if p in ("SPEC.md", "tests/test_model.py")]
    protected += [p for p in outcome.changed if p.startswith("data/")]
    if protected:
        return False
    path = ws / "tests" / "test_metrics.py"
    if not path.is_file():
        return False
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return False
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    tests = [f for f in funcs if f.name.startswith("test")]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    covers = {"positions", "avg_cost", "realized_pnl"} <= names
    if len(tests) < 3 or not covers:
        return False
    return run_py(ws, "-m", "pytest", "-q", "-p", "no:cacheprovider").returncode == 0


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "trades.csv").write_text(TRADES)
        (d / "prices.csv").write_text(PRICES)
        (d / "prices_no_jpm.csv").write_text(PRICES_NO_JPM)
        trades, prices = str(d / "trades.csv"), str(d / "prices.csv")

        r = run_py(ws, "-c", PROBE, trades, prices)
        if r.returncode != 0:
            return False, f"0/10 · probe crashed: {r.stderr.strip()[-200:]}"
        got = json.loads(r.stdout)
        r1 = got["R1"]
        ok: dict[str, bool] = {}
        ok["R1"] = (
            isinstance(r1, dict)
            and "n" in r1
            and r1["sorted"]
            and r1["datetimes"]
            and r1["types"]
            and r1["first"] == ["AAPL", "BUY", 100, 170.0]
            and r1["symbols"] == ["AAPL", "JPM", "MSFT", "XOM"]
            and r1["sides"] == ["BUY", "SELL"]
        )
        ok["R2"] = isinstance(r1, dict) and r1.get("n") == 8 and r1.get("rejected") == 4
        ok["R3"] = _close_map(got["R3"], {"AAPL": 181.0, "MSFT": 415.5, "JPM": 200.0, "XOM": 116.0})
        ok["R4"] = got["R4"] == POSITIONS
        ok["R5"] = _close_map(got["R5"], AVG)
        ok["R6"] = _close_map(got["R6"], PNL) and _close(got["R6b"], -50.0)
        ok["R7"] = _close_map(got["R7"], EXPOSURE) and got["R7b"] is True

        code, report, _ = _cli(ws, trades, prices)
        why: dict[str, str] = {}
        r8 = _report_errors(report, REPORT["top"])  # type: ignore[arg-type]
        if code != 0:
            r8.insert(0, f"exit code {code}")
        ok["R8"] = not r8
        why["R8"] = "; ".join(r8)
        code2, report2, _ = _cli(ws, trades, prices, "--top", "2")
        code3, _, err3 = _cli(ws, trades, str(d / "prices_no_jpm.csv"))
        r9 = [f"--top 2: {e}" for e in _report_errors(report2, ["AAPL", "JPM"])]
        if code2 != 0:
            r9.insert(0, f"--top 2 exit code {code2}")
        if code3 != 2 or "JPM" not in err3 or "Traceback" in err3:
            r9.append(f"missing price: exit {code3}, stderr {err3.strip()[-80:]!r}")
        ok["R9"] = not r9
        why["R9"] = "; ".join(r9)
        ok["R10"] = _tests_ok(ws, outcome)

    failed = [k for k in REQUIREMENTS if not ok[k]]
    passed = len(REQUIREMENTS) - len(failed)
    reason = f"{passed}/10" + (f" · failed {' '.join(failed)}" if failed else " · all met")
    details = [f"{k}: {why[k]}" for k in failed if why.get(k)]
    return not failed, reason + "".join(f" · {d}" for d in details)
