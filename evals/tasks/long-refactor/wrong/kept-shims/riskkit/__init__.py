"""riskkit: small risk analytics for the desk."""

from riskkit.measures import ExpectedShortfall, VaR

__all__ = ["ExpectedShortfall", "VaR", "legacy_es", "legacy_var"]
__version__ = "1.9.0"


def legacy_var(returns: list[float], conf: float = 0.95) -> float:
    """Deprecated: use -VaR(conf).of(returns)."""
    return -VaR(conf).of(returns)


def legacy_es(returns: list[float], conf: float = 0.95) -> float:
    """Deprecated: use -ExpectedShortfall(conf).of(returns)."""
    return -ExpectedShortfall(conf).of(returns)
