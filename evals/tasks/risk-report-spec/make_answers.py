"""Regenerates wrong/ and alt/ from oracle/. Each wrong/ answer is the oracle with one
realistic mistake, so each proves the checker catches that mistake and nothing else;
`stopped-early` is the failure this task exists for. Re-run after editing oracle/,
then `make evals-selftest`:

    uv run python evals/tasks/risk-report-spec/make_answers.py
"""

import shutil
from pathlib import Path

HERE = Path(__file__).parent
ORACLE = HERE / "oracle"


def sub(text: str, old: str, new: str) -> str:
    assert old in text, f"edit no longer applies: {old!r}"
    return text.replace(old, new)


def variant(rel: str, edits: dict[str, list[tuple[str, str]]], drop: tuple[str, ...] = ()) -> None:
    """Copy oracle/ to `rel`, apply (old, new) edits per file, remove `drop`ped files."""
    d = HERE / rel
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(ORACLE, d)
    for name, pairs in edits.items():
        path = d / name
        text = path.read_text() if path.exists() else ""
        for old, new in pairs:
            text = sub(text, old, new) if old else new
        path.write_text(text)
    for name in drop:
        (d / name).unlink()


MAIN, IO, METRICS = "riskreport/__main__.py", "riskreport/io.py", "riskreport/metrics.py"


def main() -> None:
    # Declared done after R8: no --top, a traceback on a missing price, no tests.
    variant(
        "wrong/stopped-early",
        {
            MAIN: [
                ('    ap.add_argument("--top", type=int, default=3)\n', ""),
                (
                    "    try:\n        mv = exposure(pos, load_prices(args.prices))\n"
                    "    except KeyError as e:\n"
                    '        print(f"riskreport: {e.args[0]}", file=sys.stderr)\n'
                    "        return 2\n",
                    "    mv = exposure(pos, load_prices(args.prices))\n",
                ),
                ("ranked[: args.top]", "ranked[:3]"),
            ]
        },
        drop=("tests/test_metrics.py",),
    )
    variant(
        "wrong/average-cost-pnl",
        {
            METRICS: [
                (
                    "def realized_pnl(trades: list[Trade], symbol: str) -> float:\n",
                    "def realized_pnl(trades: list[Trade], symbol: str) -> float:\n"
                    "    cost = avg_cost(trades, symbol)\n"
                    "    return sum((t.price - cost) * t.qty for t in trades\n"
                    '               if t.symbol == symbol and t.side == "SELL")\n\n\n'
                    "def _fifo_unused(trades: list[Trade], symbol: str) -> float:\n",
                ),
            ]
        },
    )
    # Observed in the wild: Haiku 4.5 did this in 6/6 trials (S3 A/B). Summing realized
    # P&L over open positions drops fully closed ones, which still carry realized P&L.
    variant(
        "wrong/realized-over-open-positions",
        {
            MAIN: [
                (
                    "sum(realized_pnl(trades, s) for s in {t.symbol for t in trades})",
                    "sum(realized_pnl(trades, s) for s in pos)",
                )
            ]
        },
    )
    variant(
        "wrong/traceback-on-missing-price",
        {
            MAIN: [
                (
                    "    except KeyError as e:",
                    "    except LookupError as e:\n        raise\n    except ArithmeticError as e:",
                )
            ]
        },
    )
    variant(
        "wrong/bad-row-raises",
        {
            IO: [
                (
                    "            except (KeyError, TypeError, ValueError, AttributeError):\n"
                    "                rejected += 1\n                continue\n",
                    "            except KeyError:\n                rejected += 1\n"
                    "                continue\n",
                )
            ]
        },
    )
    variant("wrong/case-sensitive-side", {IO: [('row["side"].strip().upper()', 'row["side"]')]})
    variant(
        "wrong/keeps-flat-positions",
        {METRICS: [("return {s: q for s, q in net.items() if q != 0}", "return dict(net)")]},
    )
    variant(
        "wrong/edited-model-test",
        {"tests/test_model.py": [("", "def test_trade():\n    assert True\n")]},
    )
    # Correct, written differently: index-based FIFO, pretty-printed JSON, unittest-style
    # tests calling through the module. Proves the checker grades behaviour, not style.
    variant(
        "alt/different-style",
        {
            MAIN: [("print(json.dumps(report))", "print(json.dumps(report, indent=2))")],
            METRICS: [
                (
                    "def realized_pnl(trades: list[Trade], symbol: str) -> float:\n",
                    "def realized_pnl(trades: list[Trade], symbol: str) -> float:\n"
                    "    mine = [t for t in trades if t.symbol == symbol]\n"
                    "    mine.sort(key=lambda t: t.ts)\n"
                    '    buys = [[t.qty, t.price] for t in mine if t.side == "BUY"]\n'
                    "    i, pnl = 0, 0.0\n"
                    "    for s in mine:\n"
                    '        if s.side != "SELL":\n'
                    "            continue\n"
                    "        left = s.qty\n"
                    "        while left:\n"
                    "            take = min(left, buys[i][0])\n"
                    "            pnl += take * (s.price - buys[i][1])\n"
                    "            buys[i][0] -= take\n"
                    "            left -= take\n"
                    "            i += buys[i][0] == 0\n"
                    "    return pnl\n\n\n"
                    "def _deque_version(trades: list[Trade], symbol: str) -> float:\n",
                ),
            ],
            "tests/test_metrics.py": [
                (
                    "",
                    "import unittest\nfrom datetime import datetime\n\n"
                    "from riskreport import metrics\nfrom riskreport.model import Trade\n\n"
                    "D = datetime(2024, 1, 2)\n"
                    'T = [Trade(D, "A", "BUY", 4, 10.0),\n'
                    '     Trade(D.replace(hour=1), "A", "SELL", 4, 12.0)]\n\n\n'
                    "class MetricsTest(unittest.TestCase):\n"
                    "    def test_flat_position_dropped(self):\n"
                    "        self.assertEqual(metrics.positions(T), {})\n\n"
                    "    def test_avg_cost(self):\n"
                    '        self.assertEqual(metrics.avg_cost(T, "A"), 10.0)\n\n'
                    "    def test_pnl(self):\n"
                    '        self.assertEqual(metrics.realized_pnl(T, "A"), 8.0)\n',
                ),
            ],
        },
    )


if __name__ == "__main__":
    main()
