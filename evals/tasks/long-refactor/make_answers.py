"""Regenerates oracle/, wrong/ and alt/ from fixture/ by rewriting exactly the call
sites make_fixture.py wrote. Each wrong/ is one thing a migration can lose (often the
detail a compacted history forgets: the sign, the default, a hidden call site).

    uv run python evals/tasks/long-refactor/make_fixture.py   # first, if it changed
    uv run python evals/tasks/long-refactor/make_answers.py
"""

import json
import shutil
from pathlib import Path

HERE = Path(__file__).parent
FIX = HERE / "fixture"

# file -> [(old, new)], the oracle migration
ORACLE: dict[str, list[tuple[str, str]]] = {
    "riskkit/__init__.py": [
        ("from riskkit.legacy import legacy_es, legacy_var  # re-exported for old callers\n", ""),
        (
            '__all__ = ["ExpectedShortfall", "VaR", "legacy_es", "legacy_var"]',
            '__all__ = ["ExpectedShortfall", "VaR"]',
        ),
    ],
    "riskkit/desk_report.py": [
        ("from riskkit.legacy import legacy_var", "from riskkit.measures import VaR"),
        ("return legacy_var(returns, 0.99)", "return -VaR(0.99).of(returns)"),
    ],
    "riskkit/limits.py": [
        (
            "from riskkit.legacy import legacy_es, legacy_var",
            "from riskkit.measures import ExpectedShortfall, VaR",
        ),
        ("return legacy_var(returns, conf=conf) > limit", "return -VaR(conf).of(returns) > limit"),
        ("return limit - legacy_es(returns)", "return limit + ExpectedShortfall(0.95).of(returns)"),
    ],
    "riskkit/stress.py": [
        ("from riskkit import legacy", "from riskkit.measures import ExpectedShortfall"),
        ("return legacy.legacy_es(shocked, 0.975)", "return -ExpectedShortfall(0.975).of(shocked)"),
    ],
    "riskkit/portfolio.py": [
        ("from riskkit.legacy import legacy_var as lv", "from riskkit.measures import VaR"),
        ("{name: lv(rets, conf) for", "{name: -VaR(conf).of(rets) for"),
    ],
    "riskkit/backtest.py": [
        ("from riskkit.legacy import legacy_var", "from riskkit.measures import VaR"),
        ("[legacy_var(returns[i - window : i]) for", "[-VaR(0.95).of(returns[i - window : i]) for"),
        ("legacy_var(returns[i - window : i], conf)", "-VaR(conf).of(returns[i - window : i])"),
    ],
    "riskkit/attribution.py": [
        ("from riskkit.legacy import legacy_es", "from riskkit.measures import ExpectedShortfall"),
        (
            "return legacy_es(desk, conf) - legacy_es(without, conf=conf)",
            "return ExpectedShortfall(conf).of(without) - ExpectedShortfall(conf).of(desk)",
        ),
    ],
    "docs/MIGRATION.md": [("- [ ]", "- [x]")],
    "CHANGELOG.md": [
        (
            "## Unreleased\n",
            "## Unreleased\n\n- Remove riskkit.legacy; all callers use riskkit.measures.\n",
        ),
    ],
}


def migrated(edits: dict[str, list[tuple[str, str]]]) -> dict[str, str]:
    out = {}
    for rel, subs in edits.items():
        text = (FIX / rel).read_text()
        for old, new in subs:
            assert old in text, (rel, old)
            text = text.replace(old, new)
        out[rel] = text
    return out


def write(
    rel: str, files: dict[str, str], delete: tuple[str, ...] = ("riskkit/legacy.py",)
) -> None:
    d = HERE / rel
    shutil.rmtree(d, ignore_errors=True)
    if delete:
        files = {**files, "_delete.txt": "\n".join(delete) + "\n"}
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)


def main() -> None:
    write("oracle", migrated(ORACLE))
    # the sign: measures return P&L (negative for a loss); legacy returned positive losses
    write(
        "wrong/sign-not-flipped",
        migrated(
            {
                **ORACLE,
                "riskkit/desk_report.py": [
                    ORACLE["riskkit/desk_report.py"][0],
                    ("return legacy_var(returns, 0.99)", "return VaR(0.99).of(returns)"),
                ],
            }
        ),
    )
    # the default: legacy_var's conf defaulted to 0.95; measures have no default
    write(
        "wrong/default-conf-guessed",
        migrated(
            {
                **ORACLE,
                "riskkit/backtest.py": [
                    ORACLE["riskkit/backtest.py"][0],
                    (
                        "[legacy_var(returns[i - window : i]) for",
                        "[-VaR(0.99).of(returns[i - window : i]) for",
                    ),
                    ORACLE["riskkit/backtest.py"][2],
                ],
            }
        ),
    )
    # the alias call site missed: legacy.py is gone, so portfolio no longer imports
    write("wrong/alias-call-site-missed", migrated({**ORACLE, "riskkit/portfolio.py": []}))
    # the re-export in __init__ left: deleting legacy.py breaks every import
    write("wrong/init-still-reexports", migrated({**ORACLE, "riskkit/__init__.py": []}))
    # everything migrated, but the old module kept
    write("wrong/legacy-file-kept", migrated(ORACLE), delete=())
    write("wrong/no-changelog", migrated({k: v for k, v in ORACLE.items() if k != "CHANGELOG.md"}))
    write(
        "wrong/checklist-not-ticked",
        migrated({k: v for k, v in ORACLE.items() if k != "docs/MIGRATION.md"}),
    )
    # stopped after half the modules (the plan lost in a compaction)
    half = {
        k: v
        for k, v in ORACLE.items()
        if k not in ("riskkit/backtest.py", "riskkit/attribution.py")
    }
    write("wrong/stopped-halfway", migrated(half))
    tests = (FIX / "tests/test_riskkit.py").read_text().replace("< 25", "< 400")
    write("wrong/edited-tests", {**migrated(ORACLE), "tests/test_riskkit.py": tests})
    # the names kept as deprecated shims in __init__ (s6-full rep 1): results unchanged,
    # but the goal says to remove them entirely
    shims = (
        '"""riskkit: small risk analytics for the desk."""\n\n'
        "from riskkit.measures import ExpectedShortfall, VaR\n\n"
        '__all__ = ["ExpectedShortfall", "VaR", "legacy_es", "legacy_var"]\n'
        '__version__ = "1.9.0"\n\n\n'
        "def legacy_var(returns: list[float], conf: float = 0.95) -> float:\n"
        '    """Deprecated: use -VaR(conf).of(returns)."""\n'
        "    return -VaR(conf).of(returns)\n\n\n"
        "def legacy_es(returns: list[float], conf: float = 0.95) -> float:\n"
        '    """Deprecated: use -ExpectedShortfall(conf).of(returns)."""\n'
        "    return -ExpectedShortfall(conf).of(returns)\n"
    )
    write("wrong/kept-shims", {**migrated(ORACLE), "riskkit/__init__.py": shims})
    write(
        "wrong/overflowed",
        {**migrated(ORACLE), "_runs.json": json.dumps([{"status": "context_overflow"}])},
    )
    # a different, equally correct style: named measures and a sign helper
    alt = migrated(ORACLE)
    alt["riskkit/desk_report.py"] = (
        (FIX / "riskkit/desk_report.py")
        .read_text()
        .replace(
            "from riskkit.legacy import legacy_var",
            "from riskkit.measures import VaR\n\n_DESK_VAR = VaR(conf=0.99)\n\n\n"
            "def _as_loss(pnl: float) -> float:\n"
            '    """measures return P&L (negative = loss); this module reports losses."""\n'
            "    return -pnl",
        )
        .replace("return legacy_var(returns, 0.99)", "return _as_loss(_DESK_VAR.of(returns))")
    )
    write("alt/named-measures", alt)


if __name__ == "__main__":
    main()
