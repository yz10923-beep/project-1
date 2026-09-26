"""riskcalc command-line interface."""

import argparse
import sys

import riskcalc
from riskcalc.var import historical_var


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("--version", "-V"):
        print(f"riskcalc version {riskcalc.__version__}")
        return 0
    parser = argparse.ArgumentParser(prog="riskcalc", description="Desk risk calculations.")
    sub = parser.add_subparsers(dest="command", required=True)
    var = sub.add_parser("var", help="one-day historical VaR from a file of daily returns")
    var.add_argument("returns_file")
    var.add_argument("--confidence", type=float, default=0.99)
    args = parser.parse_args(argv)

    if args.command == "var":
        with open(args.returns_file) as fh:
            returns = [float(line) for line in fh if line.strip()]
        print(f"VaR({args.confidence:.0%}) = {historical_var(returns, args.confidence):.4%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
