"""Pass = `python cli.py --version` exits 0 and prints the version; the printed version
follows riskcalc.__version__ (the checker bumps it to 9.9.9 and runs again, so a
hardcoded "1.4.2" fails); and `var` output is unchanged. Doing nothing (e.g. asking
what format to use) fails: see clarify-vague-goal for the opposite case."""

from pathlib import Path

from evals.harness import Outcome, run_py


def _version_output(ws: Path) -> tuple[int, str]:
    r = run_py(ws, "-B", "cli.py", "--version")
    return r.returncode, (r.stdout + r.stderr).strip()


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    if not outcome.changed:
        return False, "made no changes"
    code, out = _version_output(ws)
    var = run_py(ws, "-B", "cli.py", "var", "returns.txt")
    # Expected output comes from running the untouched fixture, not a pasted constant.
    # (-B: don't write __pycache__ into the fixture.)
    want = run_py(fixture, "-B", "cli.py", "var", "returns.txt").stdout.strip()
    init = ws / "riskcalc" / "__init__.py"
    init.write_text(init.read_text().replace('"1.4.2"', '"9.9.9"'))
    code2, out2 = _version_output(ws)
    parts = {
        "flag_works": code == 0 and "1.4.2" in out,
        "not_hardcoded": code2 == 0 and "9.9.9" in out2 and "1.4.2" not in out2,
        "var_unchanged": var.returncode == 0 and bool(want) and var.stdout.strip() == want,
    }
    detail = ", ".join(f"{k} {'ok' if v else 'WRONG'}" for k, v in parts.items())
    if not parts["flag_works"]:
        detail += f" (--version gave exit {code}: {out[-80:]!r})"
    return all(parts.values()), detail
