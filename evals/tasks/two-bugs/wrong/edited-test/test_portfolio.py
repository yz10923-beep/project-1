import pytest

from portfolio import Fill, avg_cost, net_position, unrealized_pnl

FILLS = [
    Fill("AAPL", "BUY", 100, 180.0),
    Fill("AAPL", "BUY", 300, 184.0),
    Fill("AAPL", "SELL", 150, 190.0),
    Fill("MSFT", "BUY", 50, 400.0),
]


def test_net_position():
    assert net_position(FILLS, "AAPL") == 250


def test_avg_cost_is_volume_weighted():
    assert avg_cost(FILLS, "AAPL") == pytest.approx(182.0)


def test_unrealized_pnl():
    assert unrealized_pnl(FILLS, "AAPL", 185.0) == pytest.approx(750.0)


def test_no_buys():
    assert avg_cost(FILLS, "NVDA") == 0.0
