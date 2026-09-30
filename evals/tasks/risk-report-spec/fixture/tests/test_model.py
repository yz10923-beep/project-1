import dataclasses
from datetime import datetime

import pytest

from riskreport.model import Trade


def test_trade_is_immutable():
    t = Trade(datetime(2024, 1, 2, 9, 30), "AAPL", "BUY", 10, 170.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.qty = 5
