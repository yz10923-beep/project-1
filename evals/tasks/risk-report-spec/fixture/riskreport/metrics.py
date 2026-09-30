from riskreport.model import Trade


def positions(trades: list[Trade]) -> dict[str, int]:
    raise NotImplementedError("R4")


def avg_cost(trades: list[Trade], symbol: str) -> float:
    raise NotImplementedError("R5")


def realized_pnl(trades: list[Trade], symbol: str) -> float:
    raise NotImplementedError("R6")


def exposure(positions: dict[str, int], prices: dict[str, float]) -> dict[str, float]:
    raise NotImplementedError("R7")
