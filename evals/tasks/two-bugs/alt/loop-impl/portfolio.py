"""Position and P&L helpers for the desk's end-of-day report."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str  # "BUY" or "SELL"
    qty: int
    price: float


def net_position(fills: list[Fill], symbol: str) -> int:
    """Shares held: buys add, sells subtract."""
    return sum(f.qty if f.side == "BUY" else -f.qty for f in fills if f.symbol == symbol)


def avg_cost(fills: list[Fill], symbol: str) -> float:
    """Volume-weighted average price of the BUY fills."""
    buys = [f for f in fills if f.symbol == symbol and f.side == "BUY"]
    if not buys:
        return 0.0
    notional = 0.0
    shares = 0
    for f in buys:
        notional += f.price * f.qty
        shares += f.qty
    return notional / shares


def unrealized_pnl(fills: list[Fill], symbol: str, mark: float) -> float:
    """Mark-to-market P&L of the open position against its average cost."""
    return net_position(fills, symbol) * (mark - avg_cost(fills, symbol))
