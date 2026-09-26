"""Pass = fx_rate exists and conv_rate is gone (no alias), the protected lookalike names
still exist, behavior is unchanged including the report's by-name call, the test suite
passes, no Python code still refers to conv_rate, and the README documents fx_rate.

"No Python code refers to conv_rate" is checked on tokens (names and string literals),
not raw text, so a comment or changelog mentioning the old name is fine (see alt/)."""

import io
import json
import tokenize
from pathlib import Path

from evals.harness import Outcome, run_py

PROBE = """
import json
out = {}
from pricing import fx
out["renamed"] = hasattr(fx, "fx_rate") and not hasattr(fx, "conv_rate")
protected = ("inv_conv_rate", "conv_rate_cache", "CONV_RATE_TTL")
out["protected_names"] = all(hasattr(fx, n) for n in protected)
errors = []
try:
    from pricing.quote import quote_in
    from pricing.risk import usd_exposure
    out["behavior"] = (
        fx.fx_rate("EUR", "USD") == 1.0850
        and abs(fx.fx_rate("JPY", "USD") - 1 / 151.20) < 1e-12
        and abs(fx.inv_conv_rate("GBP", "USD") - 1 / 1.2710) < 1e-12
        and quote_in(250, "EUR", "USD") == 271.25
        and usd_exposure({"GBP": 100, "EUR": 100}) == 235.6
    )
except Exception as e:
    out["behavior"] = False
    errors.append(f"behavior: {type(e).__name__}: {e}")
try:
    from reports.daily import build
    out["report"] = build() == [
        "EUR/USD fx_rate 1.0850",
        "GBP/USD fx_rate 1.2710",
        "USD/JPY inv_conv_rate 0.0066",
    ]
except Exception as e:
    out["report"] = False
    errors.append(f"report: {type(e).__name__}: {e}")
if errors:
    out["error"] = "; ".join(errors)
print(json.dumps(out))
"""


def _old_name_refs(ws: Path) -> list[str]:
    hits = []
    for path in sorted(ws.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for tok in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
            is_name = tok.type == tokenize.NAME and tok.string == "conv_rate"
            is_str = tok.type == tokenize.STRING and tok.string.strip("'\"") == "conv_rate"
            if is_name or is_str:
                hits.append(f"{path.relative_to(ws)}:{tok.start[0]}")
    return hits


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    r = run_py(ws, "-c", PROBE)
    if r.returncode != 0:
        return False, f"pricing no longer imports: {r.stderr.strip().splitlines()[-1:]}"
    got = json.loads(r.stdout)
    tests = run_py(ws, "-m", "pytest", "-q", "-p", "no:cacheprovider", timeout=120)
    readme = (ws / "README.md").read_text() if (ws / "README.md").is_file() else ""
    refs = _old_name_refs(ws)
    parts = {
        "renamed_no_alias": got["renamed"],
        "protected_names": got["protected_names"],
        "behavior": got["behavior"],
        "report": got["report"],
        "tests_pass": tests.returncode == 0,
        "no_old_refs": not refs,
        "docs": "fx_rate(base, quote)" in readme,
    }
    detail = ", ".join(f"{k} {'ok' if v else 'WRONG'}" for k, v in parts.items())
    if refs:
        detail += f" (conv_rate still at {refs[:3]})"
    if "error" in got:
        detail += f" ({got['error']})"
    return all(parts.values()), detail
