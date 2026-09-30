from riskreport.model import Trade


def load_trades(path: str) -> tuple[list[Trade], int]:
    raise NotImplementedError("R1, R2")


def load_prices(path: str) -> dict[str, float]:
    raise NotImplementedError("R3")
