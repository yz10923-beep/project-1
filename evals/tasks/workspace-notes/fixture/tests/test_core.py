import pytest

from pnlcalc.core import collateral_value, load_haircuts, margin_call


@pytest.fixture
def haircuts():
    return load_haircuts()


def test_schedule_is_current(haircuts):
    assert haircuts["version"] == 2


def test_equity_haircut(haircuts):
    assert collateral_value(1_000.0, "equity", haircuts) == pytest.approx(700.0)


def test_govt_haircut(haircuts):
    assert collateral_value(1_000.0, "govt", haircuts) == pytest.approx(980.0)


def test_corp_haircut(haircuts):
    assert collateral_value(500.0, "corp", haircuts) == pytest.approx(450.0)


def test_margin_call_shortfall():
    assert margin_call(1_000.0, 700.0) == pytest.approx(300.0)


def test_margin_call_covered():
    assert margin_call(500.0, 700.0) == 0.0


def test_margin_call_threshold():
    assert margin_call(1_000.0, 950.0, threshold=100.0) == 0.0


def test_unknown_asset_class(haircuts):
    with pytest.raises(KeyError):
        collateral_value(1.0, "crypto", haircuts)


def test_haircuts_are_fractions(haircuts):
    assert all(0 <= v < 1 for k, v in haircuts.items() if k != "version")
