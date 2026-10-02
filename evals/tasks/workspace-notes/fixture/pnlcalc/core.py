import os
from pathlib import Path


def load_haircuts() -> dict[str, float]:
    """Haircut per asset class, from the risk database named by $RISK_DB."""
    path = Path(os.environ["RISK_DB"])
    table: dict[str, float] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=")
            table[key.strip()] = float(value)
    return table


def collateral_value(market_value: float, asset_class: str, haircuts: dict[str, float]) -> float:
    return market_value * (1 - haircuts[asset_class])


def margin_call(exposure: float, collateral: float, threshold: float = 0.0) -> float:
    """How much more collateral is needed; 0 if covered (within the threshold)."""
    shortfall = exposure - collateral
    return shortfall if shortfall > threshold else 0.0
