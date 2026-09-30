import argparse
import json
import sys

from riskreport.io import load_prices, load_trades
from riskreport.metrics import exposure, positions, realized_pnl


def main() -> int:
    ap = argparse.ArgumentParser(prog="riskreport")
    ap.add_argument("trades")
    ap.add_argument("prices")
    args = ap.parse_args()
    trades, rejected = load_trades(args.trades)
    pos = positions(trades)
    mv = exposure(pos, load_prices(args.prices))
    ranked = sorted(mv, key=lambda s: (-abs(mv[s]), s))
    report = {
        "positions": pos,
        "gross_exposure": round(sum(abs(v) for v in mv.values()), 2),
        "realized_pnl": round(sum(realized_pnl(trades, s) for s in {t.symbol for t in trades}), 2),
        "top": ranked[:3],
        "rejected_rows": rejected,
    }
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
