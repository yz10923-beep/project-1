"""riskkit: small risk analytics for the desk."""

from riskkit.legacy import legacy_es, legacy_var  # re-exported for old callers
from riskkit.measures import ExpectedShortfall, VaR

__all__ = ["ExpectedShortfall", "VaR", "legacy_es", "legacy_var"]
__version__ = "1.9.0"
