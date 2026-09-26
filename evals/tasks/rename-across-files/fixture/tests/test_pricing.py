from pricing.fx import conv_rate, inv_conv_rate
from pricing.quote import quote_in
from pricing.risk import usd_exposure
from reports.daily import build


def test_direct_and_inverse_rates():
    assert conv_rate("EUR", "USD") == 1.0850
    assert round(conv_rate("USD", "EUR"), 6) == round(1 / 1.0850, 6)
    assert round(inv_conv_rate("EUR", "USD"), 6) == round(1 / 1.0850, 6)


def test_quote_and_exposure():
    assert quote_in(100, "GBP", "USD") == 127.1
    assert usd_exposure({"EUR": 1000, "USD": 500}) == 1585.0


def test_report_has_every_column():
    assert len(build()) == 3
