from datetime import datetime

from riskreport.metrics import avg_cost, positions, realized_pnl
from riskreport.model import Trade


def t(minute, symbol, side, qty, price):
    return Trade(datetime(2024, 1, 2, 10, minute), symbol, side, qty, price)


TRADES = [
    t(0, "A", "BUY", 10, 100.0),
    t(1, "A", "BUY", 10, 110.0),
    t(2, "A", "SELL", 15, 120.0),
    t(3, "B", "BUY", 5, 50.0),
    t(4, "B", "SELL", 5, 55.0),
]


def test_positions_nets_and_drops_flat():
    assert positions(TRADES) == {"A": 5}


def test_avg_cost_weights_buys_only():
    assert avg_cost(TRADES, "A") == 105.0
    assert avg_cost(TRADES, "Z") == 0.0


def test_realized_pnl_is_fifo():
    # 10 from the 100 lot, 5 from the 110 lot
    assert realized_pnl(TRADES, "A") == 10 * 20 + 5 * 10
