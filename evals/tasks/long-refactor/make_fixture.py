"""Writes fixture/: the `riskkit` package before the migration. Generated so the size is
deliberate (each module carries ordinary helper code around its legacy call sites, so
reading and rewriting all of them outgrows the task's budget) and reproducible. The
call sites are written out literally below; make_answers.py rewrites exactly these.

    uv run python evals/tasks/long-refactor/make_fixture.py
"""

import random
import shutil
import textwrap
from pathlib import Path

HERE = Path(__file__).parent
FIX = HERE / "fixture"

LEGACY = '''\
"""Deprecated risk measures, kept for old callers. Use riskkit.measures instead.

Both functions return a LOSS as a positive number (a 2% loss is 0.02).
"""


def legacy_var(returns: list[float], conf: float = 0.95) -> float:
    """Historical value-at-risk at `conf`, as a positive loss."""
    xs = sorted(returns)
    k = int((1 - conf) * len(xs))
    return -xs[k]


def legacy_es(returns: list[float], conf: float = 0.95) -> float:
    """Historical expected shortfall at `conf`: the mean loss beyond VaR, positive."""
    xs = sorted(returns)
    k = max(1, int((1 - conf) * len(xs)))
    return -sum(xs[:k]) / k
'''

MEASURES = '''\
"""Risk measures (riskkit 2.x).

Sign convention: every measure returns a quantile or mean of P&L RETURNS, so a loss is
NEGATIVE (a 2% loss is -0.02). This is the opposite of riskkit.legacy, whose functions
returned losses as positive numbers. There is no default confidence level: pass it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VaR:
    """Historical value-at-risk: the (1 - conf) quantile of returns."""

    conf: float

    def of(self, returns: list[float]) -> float:
        xs = sorted(returns)
        k = int((1 - self.conf) * len(xs))
        return xs[k]


@dataclass(frozen=True)
class ExpectedShortfall:
    """Historical expected shortfall: the mean of the returns at or below VaR."""

    conf: float

    def of(self, returns: list[float]) -> float:
        xs = sorted(returns)
        k = max(1, int((1 - self.conf) * len(xs)))
        return sum(xs[:k]) / k
'''

INIT = '''\
"""riskkit: small risk analytics for the desk."""

from riskkit.legacy import legacy_es, legacy_var  # re-exported for old callers
from riskkit.measures import ExpectedShortfall, VaR

__all__ = ["ExpectedShortfall", "VaR", "legacy_es", "legacy_var"]
__version__ = "1.9.0"
'''

# module -> (docstring, imports, call-site code). Every legacy call site in the package.
MODULES: dict[str, tuple[str, str, str]] = {
    "desk_report": (
        "Daily desk risk report figures.",
        "from riskkit.legacy import legacy_var",
        '''
        def desk_var(returns: list[float]) -> float:
            """The desk's headline 99% VaR, as a positive loss."""
            return legacy_var(returns, 0.99)
        ''',
    ),
    "limits": (
        "Limit checks against VaR and ES.",
        "from riskkit.legacy import legacy_es, legacy_var",
        '''
        def breaches_var_limit(returns: list[float], limit: float, conf: float = 0.975) -> bool:
            """True when VaR (a positive loss) is above the limit."""
            return legacy_var(returns, conf=conf) > limit


        def es_headroom(returns: list[float], limit: float) -> float:
            """How much ES (95%, positive loss) is left under the limit."""
            return limit - legacy_es(returns)
        ''',
    ),
    "stress": (
        "Stress scenarios: shocked return series.",
        "from riskkit import legacy",
        '''
        def stressed_es(returns: list[float], shock: float) -> float:
            """97.5% ES of the returns scaled by `shock`, as a positive loss."""
            shocked = [r * shock for r in returns]
            return legacy.legacy_es(shocked, 0.975)
        ''',
    ),
    "portfolio": (
        "Per-book risk for a portfolio of books.",
        "from riskkit.legacy import legacy_var as lv",
        '''
        def book_vars(books: dict[str, list[float]], conf: float = 0.99) -> dict[str, float]:
            """VaR (positive loss) per book."""
            return {name: lv(rets, conf) for name, rets in books.items()}


        def worst_book(books: dict[str, list[float]]) -> str:
            """The book with the largest 99% VaR."""
            vars_ = book_vars(books)
            return max(vars_, key=lambda name: vars_[name])
        ''',
    ),
    "backtest": (
        "VaR backtesting: exceptions over a rolling window.",
        "from riskkit.legacy import legacy_var",
        '''
        def var_series(returns: list[float], window: int = 60) -> list[float]:
            """Rolling 95% VaR (positive loss) from the previous `window` days."""
            return [legacy_var(returns[i - window : i]) for i in range(window, len(returns))]


        def exceptions(returns: list[float], window: int = 60, conf: float = 0.99) -> int:
            """Days whose loss exceeded the VaR estimated from the window before them."""
            count = 0
            for i in range(window, len(returns)):
                if -returns[i] > legacy_var(returns[i - window : i], conf):
                    count += 1
            return count
        ''',
    ),
    "attribution": (
        "Marginal contribution of a book to desk ES.",
        "from riskkit.legacy import legacy_es",
        '''
        def marginal_es(desk: list[float], book: list[float], conf: float = 0.975) -> float:
            """Change in desk ES (positive loss) from removing `book`'s returns."""
            without = [d - b for d, b in zip(desk, book, strict=True)]
            return legacy_es(desk, conf) - legacy_es(without, conf=conf)
        ''',
    ),
    "reporting": ("Formatting helpers for the report.", "", ""),
    "calendar": ("Business-day helpers.", "", ""),
}

# Ordinary helper code that makes each module realistically long. Picked per module
# with a fixed seed; `{p}` is the module's prefix.
HELPERS = [
    '''
    def {p}_to_bp(x: float) -> str:
        """Format a return as basis points, one decimal."""
        return f"{{x * 1e4:.1f}}bp"
    ''',
    '''
    def {p}_clean(returns: list[float | None]) -> list[float]:
        """Drop missing observations and anything that is not a finite number."""
        out: list[float] = []
        for r in returns:
            if r is None:
                continue
            if r != r or r in (float("inf"), float("-inf")):
                continue
            out.append(float(r))
        return out
    ''',
    '''
    def {p}_mean(xs: list[float]) -> float:
        """Arithmetic mean; 0.0 for an empty list."""
        return sum(xs) / len(xs) if xs else 0.0
    ''',
    '''
    def {p}_stdev(xs: list[float]) -> float:
        """Sample standard deviation; 0.0 for fewer than two points."""
        if len(xs) < 2:
            return 0.0
        m = sum(xs) / len(xs)
        return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5
    ''',
    '''
    def {p}_drawdown(returns: list[float]) -> float:
        """Maximum peak-to-trough drawdown of the cumulative return path."""
        peak, level, worst = 1.0, 1.0, 0.0
        for r in returns:
            level *= 1 + r
            peak = max(peak, level)
            worst = max(worst, 1 - level / peak)
        return worst
    ''',
    '''
    def {p}_table(rows: list[tuple[str, float]], width: int = 24) -> str:
        """A two-column text table, names left, values right."""
        lines = []
        for name, value in rows:
            lines.append(f"{{name:<{{width}}}}{{value:>12.6f}}")
        return "\\n".join(lines)
    ''',
    '''
    def {p}_scale(returns: list[float], target_vol: float) -> list[float]:
        """Rescale returns to a target volatility (no-op for a flat series)."""
        n = len(returns)
        if n < 2:
            return list(returns)
        m = sum(returns) / n
        vol = (sum((r - m) ** 2 for r in returns) / (n - 1)) ** 0.5
        if vol == 0:
            return list(returns)
        return [r * target_vol / vol for r in returns]
    ''',
    '''
    def {p}_window(xs: list[float], end: int, size: int) -> list[float]:
        """The `size` observations before index `end` (fewer at the start)."""
        return xs[max(0, end - size) : end]
    ''',
    '''
    def {p}_merge(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
        """Sum two per-name maps; names in either appear in the result."""
        out = dict(a)
        for k, v in b.items():
            out[k] = out.get(k, 0.0) + v
        return out
    ''',
    '''
    def {p}_validate(conf: float) -> float:
        """Reject confidence levels outside (0.5, 1)."""
        if not 0.5 < conf < 1:
            raise ValueError(f"confidence level must be in (0.5, 1), got {{conf}}")
        return conf
    ''',
    '''
    def {p}_cumulative(returns: list[float]) -> list[float]:
        """Cumulative compounded return after each day."""
        out, level = [], 1.0
        for r in returns:
            level *= 1 + r
            out.append(level - 1)
        return out
    ''',
    '''
    def {p}_summary(returns: list[float]) -> dict[str, float]:
        """Count, mean, min and max of a return series."""
        if not returns:
            return {{"n": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}}
        return {{
            "n": float(len(returns)),
            "mean": sum(returns) / len(returns),
            "min": min(returns),
            "max": max(returns),
        }}
    ''',
]

TESTS = '''\
"""The desk's own tests. They pass before and after the migration: results must not change."""

import random

from riskkit.backtest import exceptions
from riskkit.desk_report import desk_var
from riskkit.limits import breaches_var_limit, es_headroom
from riskkit.portfolio import worst_book

random.seed(7)
RETS = [random.gauss(0.0003, 0.012) for _ in range(500)]


def test_desk_var_is_a_positive_loss() -> None:
    assert 0.02 < desk_var(RETS) < 0.04


def test_limits() -> None:
    assert breaches_var_limit(RETS, 0.01)
    assert not breaches_var_limit(RETS, 0.10)
    assert 0 < es_headroom(RETS, 0.05) < 0.05


def test_worst_book() -> None:
    books = {"rates": [r * 0.5 for r in RETS], "equities": [r * 2 for r in RETS]}
    assert worst_book(books) == "equities"


def test_backtest_exceptions_are_rare() -> None:
    assert 0 <= exceptions(RETS) < 25
'''

MIGRATION = """\
# Migrating off riskkit.legacy

riskkit 2.0 removes `riskkit/legacy.py`. Every caller moves to `riskkit.measures`:

| old | new |
|---|---|
| `legacy_var(returns, conf)` | `VaR(conf).of(returns)` |
| `legacy_es(returns, conf)` | `ExpectedShortfall(conf).of(returns)` |

Read the conventions in `riskkit/measures.py` before you change a caller: results seen
by users of this package must not change.

Checklist:

- [ ] every caller in riskkit/ uses riskkit.measures
- [ ] riskkit/legacy.py deleted, and nothing imports it
- [ ] CHANGELOG.md updated under Unreleased
"""

CHANGELOG = """\
# Changelog

## Unreleased

## 1.9.0 - 2024-02-20

- Add riskkit.measures (VaR, ExpectedShortfall).
- Deprecate riskkit.legacy.

## 1.8.2 - 2024-01-11

- Fix rolling-window off-by-one in backtest.var_series.
"""


def module_text(name: str, rng: random.Random) -> str:
    doc, imports, calls = MODULES[name]
    prefix = name[:4]
    helpers = [textwrap.dedent(h).strip().format(p=prefix) for h in rng.sample(HELPERS, 9)]
    head = f'"""{doc}"""\n\nfrom __future__ import annotations\n'
    if imports:
        head += f"\n{imports}\n"
    body = [textwrap.dedent(calls).strip()] if calls.strip() else []
    return head + "\n\n" + "\n\n\n".join(body + helpers) + "\n"


def main() -> None:
    shutil.rmtree(FIX, ignore_errors=True)
    pkg = FIX / "riskkit"
    pkg.mkdir(parents=True)
    rng = random.Random(42)
    files = {
        "riskkit/__init__.py": INIT,
        "riskkit/legacy.py": LEGACY,
        "riskkit/measures.py": MEASURES,
        "tests/test_riskkit.py": TESTS,
        "docs/MIGRATION.md": MIGRATION,
        "CHANGELOG.md": CHANGELOG,
    }
    for name in MODULES:
        files[f"riskkit/{name}.py"] = module_text(name, rng)
    for rel, text in files.items():
        (FIX / rel).parent.mkdir(parents=True, exist_ok=True)
        (FIX / rel).write_text(text)


if __name__ == "__main__":
    main()
