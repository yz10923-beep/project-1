"""The desk's own tests. They pass before and after the migration: results must not change."""

import random

from riskkit.backtest import exceptions
from riskkit.desk_report import desk_var
from riskkit.limits import breaches_var_limit, es_headroom
from riskkit.portfolio import worst_book

random.seed(7)
RETS = [random.gauss(0.0003, 0.012) for _ in range(500)]


def test_desk_var_is_a_positive_loss() -> None:
    assert 0.02 < desk_var(RETS) < 0.04


def test_limits() -> None:
    assert breaches_var_limit(RETS, 0.01)
    assert not breaches_var_limit(RETS, 0.10)
    assert 0 < es_headroom(RETS, 0.05) < 0.05


def test_worst_book() -> None:
    books = {"rates": [r * 0.5 for r in RETS], "equities": [r * 2 for r in RETS]}
    assert worst_book(books) == "equities"


def test_backtest_exceptions_are_rare() -> None:
    assert 0 <= exceptions(RETS) < 25
