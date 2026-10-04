"""Pass = every requirement below holds. Graded separately (like risk-report-spec), so
a failure names what was lost:

- legacy_deleted, no_legacy_refs: legacy.py gone, and no code imports or calls it
- behavior: every public function returns what it returned before the migration
  (a probe runs against the agent's package and against a pristine copy of fixture/)
- tests_untouched, tests_pass
- changelog: an Unreleased entry that mentions the legacy removal
- checklist: every item in docs/MIGRATION.md ticked
- no_overflow
"""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from evals.harness import Outcome, run_py

PROBE = r"""
import json, random
from importlib import import_module
rng = random.Random(11)
R = [rng.gauss(0.0002, 0.013) for _ in range(400)]
B = [rng.gauss(0.0, 0.006) for _ in range(400)]
out = {}
def call(name, f):
    try:
        out[name] = f()
    except Exception as e:
        out[name] = f"error: {type(e).__name__}"
m = lambda n: import_module("riskkit." + n)
call("desk_var", lambda: m("desk_report").desk_var(R))
call("breaches_975", lambda: m("limits").breaches_var_limit(R, 0.02))
call("breaches_99", lambda: m("limits").breaches_var_limit(R, 0.03, conf=0.99))
call("es_headroom", lambda: m("limits").es_headroom(R, 0.05))
call("stressed_es", lambda: m("stress").stressed_es(R, 1.5))
call("book_vars", lambda: m("portfolio").book_vars({"a": R, "b": B}))
call("book_vars_975", lambda: m("portfolio").book_vars({"a": R}, conf=0.975))
call("worst_book", lambda: m("portfolio").worst_book({"a": B, "b": R}))
call("var_series", lambda: m("backtest").var_series(R)[::7])
call("exceptions", lambda: m("backtest").exceptions(R))
call("marginal_es", lambda: m("attribution").marginal_es(R, B))
call("public_api", lambda: sorted(n for n in import_module("riskkit").__all__ if "legacy" not in n))
print(json.dumps(out))
"""
LEGACY_USE = re.compile(
    r"\blegacy_(?:var|es)\b|^\s*(?:from|import)\s+riskkit(?:\.legacy\b|\s+import\s+legacy\b)",
    re.MULTILINE,
)
_expected: dict[str, Any] | None = None


def _probe(root: Path) -> dict[str, Any]:
    proc = run_py(root, "-c", PROBE, timeout=60)
    try:
        return dict(json.loads(proc.stdout.strip().splitlines()[-1]))
    except (IndexError, ValueError):
        return {"probe": f"crashed: {proc.stderr.strip()[-200:]}"}


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return bool(a == b)


def expected(fixture: Path) -> dict[str, Any]:
    global _expected
    if _expected is None:
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(fixture, Path(tmp) / "ws")
            _expected = _probe(Path(tmp) / "ws")
        assert not any(str(v).startswith("error") for v in _expected.values()), _expected
    return _expected


def _unreleased(changelog: str) -> str:
    m = re.search(r"^## Unreleased\s*$(.*?)(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    parts: dict[str, bool] = {}
    notes: list[str] = []
    parts["legacy_deleted"] = not (ws / "riskkit" / "legacy.py").exists()
    users = sorted(
        p.relative_to(ws).as_posix()
        for p in (ws / "riskkit").rglob("*.py")
        if p.name != "legacy.py" and LEGACY_USE.search(p.read_text())
    )
    parts["no_legacy_refs"] = not users
    if users:
        notes.append(f"still uses legacy: {', '.join(users)}")
    want, got = expected(fixture), _probe(ws)
    differ = sorted(k for k in want if not _same(want[k], got.get(k)))
    parts["behavior"] = not differ and got.keys() == want.keys()
    if differ or got.keys() != want.keys():
        notes.append(f"results changed: {', '.join(differ or sorted(got.keys() ^ want.keys()))}")
    parts["tests_untouched"] = not any(p.startswith("tests/") for p in outcome.changed)
    tests = run_py(ws, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests", timeout=120)
    parts["tests_pass"] = tests.returncode == 0
    changelog = (ws / "CHANGELOG.md").read_text() if (ws / "CHANGELOG.md").is_file() else ""
    parts["changelog"] = "legacy" in _unreleased(changelog).lower()
    migration = ws / "docs" / "MIGRATION.md"
    text = migration.read_text() if migration.is_file() else ""
    parts["checklist"] = "- [ ]" not in text and len(re.findall(r"- \[[xX]\]", text)) >= 3
    statuses = [r.status for r in outcome.runs] or [outcome.status]
    parts["no_overflow"] = "context_overflow" not in statuses
    met = sum(parts.values())
    detail = f"{met}/{len(parts)} · " + ", ".join(
        f"{k} {'ok' if v else 'WRONG'}" for k, v in parts.items()
    )
    return met == len(parts), detail + (f" · {'; '.join(notes)}" if notes else "")
