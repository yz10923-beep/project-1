"""The market-data job runs between the two runs and refreshes the EURUSD rate."""

from pathlib import Path

EUR_TOTAL = 1_250_000.00
OLD_RATE, NEW_RATE = 1.0850, 1.0920

REFRESHED = """# Reference FX rates (USD per unit), refreshed by the market-data job.
as_of = "2024-03-18T16:00:00Z"

[rates]
EURUSD = 1.0920
GBPUSD = 1.2655
"""


def setup(ws: Path) -> None:
    pass  # fixture/ has everything for run 1


def between(ws: Path, finished: int) -> None:
    if finished == 1:
        (ws / "config" / "fx.toml").write_text(REFRESHED)
