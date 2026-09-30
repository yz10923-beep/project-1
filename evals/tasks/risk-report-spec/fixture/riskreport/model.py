from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Trade:
    ts: datetime
    symbol: str
    side: str  # "BUY" or "SELL"
    qty: int
    price: float
