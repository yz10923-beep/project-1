"""Pass = the report follows every rule of the desk-risk-report skill, with the right
numbers, and the distractor skill was not loaded.

The report is reports/desk_risk_20240322.csv; if it isn't there, the first other CSV
the run created is graded instead, so a misnamed report still shows which other rules
it met. One part per rule. Values are recomputed from the fixture (gross and net to $1,
util_pct to 0.05); the skill's own validate.py must accept the file.
"""

import csv
import re
import subprocess
import sys
from pathlib import Path

from evals.harness import LOAD_SKILL, Outcome

HERE = Path(__file__).parent
REPORT = "reports/desk_risk_20240322.csv"
HEADER = ["desk", "gross_usd", "net_usd", "var99_usd", "limit_usd", "util_pct", "status"]
VALIDATOR = HERE / "skills" / "desk-risk-report" / "validate.py"
MONEY = re.compile(r"^-?\d+$")


def truth(fixture: Path) -> dict[str, dict[str, float | str]]:
    with (fixture / "limits.csv").open() as f:
        limits = {r["desk"]: float(r["gross_limit_usd"]) for r in csv.DictReader(f)}
    with (fixture / "var.csv").open() as f:
        var = {r["desk"]: float(r["var_99_usd"]) for r in csv.DictReader(f)}
    out: dict[str, dict[str, float | str]] = {}
    with (fixture / "positions.csv").open() as f:
        for r in csv.DictReader(f):
            row = out.setdefault(r["desk"], {"gross": 0.0, "net": 0.0})
            amount = float(r["qty"]) * float(r["price_usd"])
            row["gross"] = float(row["gross"]) + amount
            row["net"] = float(row["net"]) + (-amount if r["side"] == "SHORT" else amount)
    for desk, row in out.items():
        util = float(row["gross"]) / limits[desk] * 100
        row |= {"var": var[desk], "limit": limits[desk], "util": util}
        row["status"] = "BREACH" if util > 100 else "WARN" if util >= 85 else "OK"
    return out


def _report(ws: Path, outcome: Outcome) -> Path | None:
    if (ws / REPORT).is_file():
        return ws / REPORT
    made = [p for p, how in outcome.changed.items() if how == "added" and p.endswith(".csv")]
    return ws / made[0] if made else None


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    want = truth(fixture)
    order = sorted(want, key=lambda d: -float(want[d]["util"]))
    path = _report(ws, outcome)
    rows: list[list[str]] = []
    if path is not None:
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = [[c.strip() for c in r] for r in csv.reader(f) if r]
    header_ok = bool(rows) and rows[0] == HEADER
    body = {r[0]: r for r in rows[1:] if len(r) == len(HEADER) and r[0] != "TOTAL"}

    def close(cell: str, value: float, tol: float, money: bool = True) -> bool:
        if money and not MONEY.match(cell):
            return False
        try:
            return abs(float(cell) - value) <= tol
        except ValueError:
            return False

    values_ok = header_ok and set(body) == set(want)
    status_ok = values_ok
    for desk, w in want.items():
        r = body.get(desk)
        if r is None:
            continue
        values_ok = values_ok and all(
            (
                close(r[1], float(w["gross"]), 1),
                close(r[2], float(w["net"]), 1),
                close(r[3], float(w["var"]), 1),
                close(r[4], float(w["limit"]), 1),
                close(r[5], float(w["util"]), 0.051, money=False),
            )
        )
        status_ok = status_ok and r[6] == w["status"]
    total = rows[-1] if len(rows) > 1 else []
    total_ok = (
        header_ok
        and len(total) == len(HEADER)
        and total[0] == "TOTAL"
        and close(total[1], sum(float(w["gross"]) for w in want.values()), 2)
        and close(total[2], sum(float(w["net"]) for w in want.values()), 2)
        and total[3:] == ["", "", "", ""]
    )
    validator_ok = path is not None and (
        subprocess.run(
            [sys.executable, str(VALIDATOR), str(path)], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        == "OK"
    )
    loaded = {
        str(inp.get("name"))
        for r in outcome.runs
        for name, inp in r.tool_calls
        if name == LOAD_SKILL
    }
    parts = {
        "file_name": (ws / REPORT).is_file(),
        "header": header_ok,
        "values": values_ok,
        "status": status_ok,
        "order": header_ok and [r[0] for r in rows[1:] if r[0] != "TOTAL"] == order,
        "total_row": total_ok,
        "validator": validator_ok,
        "distractor_not_loaded": "release-notes" not in loaded,
    }
    detail = ", ".join(f"{k} {'ok' if ok else 'WRONG'}" for k, ok in parts.items())
    note = "no report found" if path is None else f"graded {path.relative_to(ws)}"
    return all(parts.values()), f"{detail} · {note}"
