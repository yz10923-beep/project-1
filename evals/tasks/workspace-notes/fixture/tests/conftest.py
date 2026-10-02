import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if not os.environ.get("RISK_DB"):
        raise pytest.UsageError("RISK_DB is not set: point it at a risk database file")
