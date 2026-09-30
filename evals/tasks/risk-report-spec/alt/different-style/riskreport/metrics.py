from collections import defaultdict, deque

from riskreport.model import Trade


def positions(trades: list[Trade]) -> dict[str, int]:
    net: dict[str, int] = defaultdict(int)
    for t in trades:
        net[t.symbol] += t.qty if t.side == "BUY" else -t.qty
    return {s: q for s, q in net.items() if q != 0}


def avg_cost(trades: list[Trade], symbol: str) -> float:
    buys = [t for t in trades if t.symbol == symbol and t.side == "BUY"]
    qty = sum(t.qty for t in buys)
    return sum(t.qty * t.price for t in buys) / qty if qty else 0.0


def realized_pnl(trades: list[Trade], symbol: str) -> float:
    mine = [t for t in trades if t.symbol == symbol]
    mine.sort(key=lambda t: t.ts)
    buys = [[t.qty, t.price] for t in mine if t.side == "BUY"]
    i, pnl = 0, 0.0
    for s in mine:
        if s.side != "SELL":
            continue
        left = s.qty
        while left:
            take = min(left, buys[i][0])
            pnl += take * (s.price - buys[i][1])
            buys[i][0] -= take
            left -= take
            i += buys[i][0] == 0
    return pnl


def _deque_version(trades: list[Trade], symbol: str) -> float:
    lots: deque[list[float]] = deque()  # [remaining qty, price], oldest first
    pnl = 0.0
    for t in sorted((t for t in trades if t.symbol == symbol), key=lambda t: t.ts):
        if t.side == "BUY":
            lots.append([t.qty, t.price])
            continue
        remaining = t.qty
        while remaining > 0 and lots:
            lot = lots[0]
            used = min(remaining, lot[0])
            pnl += (t.price - lot[1]) * used
            lot[0] -= used
            remaining -= used
            if lot[0] == 0:
                lots.popleft()
    return pnl


def exposure(positions: dict[str, int], prices: dict[str, float]) -> dict[str, float]:
    out = {}
    for symbol, qty in positions.items():
        if symbol not in prices:
            raise KeyError(f"no price for {symbol}")
        out[symbol] = qty * prices[symbol]
    return out
