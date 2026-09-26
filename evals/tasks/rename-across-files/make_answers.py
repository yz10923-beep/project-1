"""Derives oracle/, wrong/ and alt/ from fixture/ by text transforms, so they can't drift
from the fixture. Re-run after editing the fixture, then `make evals-selftest`:

    uv run python evals/tasks/rename-across-files/make_answers.py
"""

import re
import shutil
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).parent
FIXTURE = HERE / "fixture"
WORD = re.compile(r"\bconv_rate\b")  # \b: inv_conv_rate and conv_rate_cache don't match

Transform = Callable[[str, str], str]  # (relative path, text) -> new text


def precise(rel: str, text: str) -> str:
    return WORD.sub("fx_rate", text)


def build(name: str, transform: Transform) -> None:
    out = HERE / name
    shutil.rmtree(out, ignore_errors=True)
    for src in sorted(FIXTURE.rglob("*")):
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(FIXTURE).as_posix()
        text = src.read_text()
        new = transform(rel, text)
        if new != text:  # overlays carry only changed files
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_text(new)


def main() -> None:
    for d in ("wrong", "alt"):
        shutil.rmtree(HERE / d, ignore_errors=True)
    build("oracle", precise)
    # sed s/conv_rate/fx_rate/g: also renames inv_conv_rate and conv_rate_cache.
    build("wrong/blind-sed", lambda rel, t: t.replace("conv_rate", "fx_rate"))
    # Code-aware rename that skips string literals: getattr(fx, "conv_rate") breaks.
    build(
        "wrong/missed-getattr-string",
        lambda rel, t: re.sub(r'(?<!")\bconv_rate\b(?!")', "fx_rate", t),
    )
    # Renamed, but kept `conv_rate = fx_rate` for compatibility (the goal forbids it).
    build(
        "wrong/left-alias",
        lambda rel, t: (
            precise(rel, t) + ("\n\nconv_rate = fx_rate\n" if rel == "pricing/fx.py" else "")
        ),
    )
    # Code and tests renamed, docs forgotten.
    build("wrong/missed-docs", lambda rel, t: t if rel.endswith(".md") else precise(rel, t))
    # Code renamed, tests forgotten: the suite no longer imports.
    build("wrong/missed-tests", lambda rel, t: t if rel.startswith("tests/") else precise(rel, t))
    # Correct, plus a changelog note that names the old function: must still pass.
    build(
        "alt/changelog-mentions-old-name",
        lambda rel, t: (
            precise(rel, t)
            + (
                "\n## Changelog\n\n- `fx_rate` was renamed from `conv_rate`.\n"
                if rel == "README.md"
                else ""
            )
        ),
    )
    print("wrote oracle, 5 wrong, 1 alt")


if __name__ == "__main__":
    main()
