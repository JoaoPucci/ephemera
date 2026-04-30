#!/usr/bin/env python3
"""Generate a CRAP-style report for the Python codebase.

CRAP (Change Risk Anti-Patterns) combines two existing signals --
cyclomatic complexity and test coverage -- into a single score that
ranks methods by how risky they are to change. The intuition: a 2-
branch function at 100% coverage is fine; a 15-branch function at
40% coverage is a footgun. CRAP captures that with the formula:

    crap(M) = comp(M) ** 2 * (1 - cov(M)) ** 3 + comp(M)

where comp is the function's cyclomatic complexity and cov is the
fraction of its lines exercised by the test suite. Methods above a
score of ~30 are conventionally considered hard to safely change.

This script is wired in CI as informational output -- it does not
fail the build. The intent is observability: surface the top-N
risky methods every PR run so drift is visible without a refactor
gate. A future tier may threshold-block on CRAP when the codebase
has soaked at low scores for a while.

Inputs:
  - radon's JSON cyclomatic-complexity output (per function)
  - coverage.py's JSON output (per file, line-by-line)
  Both run in the calling CI step before this script. See
  .github/workflows/_test-suite.yml for the exact invocation.

Output: a markdown report on stdout, ranked by CRAP descending,
trimmed to the top --top entries (default 20).
"""

import argparse
import contextlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
COVERAGE_JSON = ROOT / "coverage.json"


def _crap(complexity: int, coverage: float) -> float:
    """Standard CRAP formula. coverage is a fraction in [0, 1]."""
    coverage = max(0.0, min(1.0, coverage))
    return complexity * complexity * (1 - coverage) ** 3 + complexity


def _radon_complexity(target: Path) -> dict[str, list[dict]]:
    """Run `python -m radon cc -j -s` and return its JSON output.

    Invoking via `python -m radon` (rather than the bare `radon` script)
    means the script works regardless of whether radon's console_script
    landed on PATH -- ./venv/bin contains radon but plain `python -m
    radon` resolves it through the same interpreter the script is
    running under, which is the assumption every Python tool can rely
    on without environment surgery.

    Output shape: { "<file>": [ {"name": "...", "complexity": N,
    "lineno": int, "endline": int, "type": "method"|"function", ... }, ... ] }
    """
    proc = subprocess.run(
        [sys.executable, "-m", "radon", "cc", "-j", "-s", str(target)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _coverage_for_lines(file_cov: dict, start: int, end: int) -> float:
    """Compute the fraction of lines in [start, end] that coverage.py
    counted as executed for this file. Lines outside coverage's
    `executed_lines` and `missing_lines` are non-executable (comments,
    blank lines, etc.) and are excluded from the denominator.
    """
    executable = set(file_cov.get("executed_lines", [])) | set(
        file_cov.get("missing_lines", [])
    )
    in_range = {n for n in executable if start <= n <= end}
    if not in_range:
        return 1.0  # nothing executable in this range -> not measurable
    executed = set(file_cov.get("executed_lines", [])) & in_range
    return len(executed) / len(in_range)


def _coverage_data() -> dict:
    if not COVERAGE_JSON.is_file():
        print(
            f"warning: {COVERAGE_JSON} not found -- run `coverage json` first",
            file=sys.stderr,
        )
        return {"files": {}}
    return json.loads(COVERAGE_JSON.read_text())


def _normalise(path: str) -> str:
    """coverage.py and radon both emit paths relative to CWD, but they
    can disagree on leading dot-slash or absoluteness. Normalise to a
    repo-relative POSIX path so the two can be cross-referenced."""
    p = Path(path)
    if p.is_absolute():
        with contextlib.suppress(ValueError):
            p = p.relative_to(ROOT)
    return p.as_posix()


def collect_rows() -> list[dict]:
    cov = _coverage_data()
    cov_by_file = {_normalise(k): v for k, v in cov.get("files", {}).items()}
    radon_by_file = _radon_complexity(APP_DIR)

    rows = []
    for raw_path, blocks in radon_by_file.items():
        rel_path = _normalise(raw_path)
        file_cov = cov_by_file.get(rel_path, {})
        for block in blocks:
            # radon emits classes too (with .complexity = sum of methods);
            # the methods themselves come through as nested entries on the
            # block. For the report we want leaf functions/methods only.
            if block.get("type") == "class":
                continue
            complexity = int(block["complexity"])
            start = int(block["lineno"])
            end = int(block.get("endline", start))
            coverage = _coverage_for_lines(file_cov, start, end)
            rows.append(
                {
                    "path": rel_path,
                    "name": block["name"],
                    "lineno": start,
                    "complexity": complexity,
                    "coverage": coverage,
                    "crap": _crap(complexity, coverage),
                }
            )
    rows.sort(key=lambda r: r["crap"], reverse=True)
    return rows


def render_markdown(rows: list[dict], top: int) -> str:
    if not rows:
        return "_No data: radon or coverage produced no rows._"

    lines = [
        "## CRAP report",
        "",
        f"Top {top} riskiest methods by CRAP score "
        "(`comp² × (1 - cov)³ + comp`). Higher is riskier; "
        "scores above ~30 are conventionally hard to change safely.",
        "",
        "Two ways to lower a method's CRAP: simplify it or cover it more.",
        "",
        "| CRAP | Complexity | Coverage | Method |",
        "| ---: | ---: | ---: | --- |",
    ]
    for row in rows[:top]:
        cov_pct = f"{row['coverage'] * 100:.0f}%"
        crap = f"{row['crap']:.1f}"
        method = f"`{row['path']}::{row['name']}`"
        lines.append(
            f"| {crap} | {row['complexity']} | {cov_pct} | {method}:{row['lineno']} |"
        )
    lines.append("")
    lines.append(
        f"_Generated by `scripts/crap_report.py` over {len(rows)} measured methods._"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of top-CRAP rows to render (default: 20).",
    )
    args = p.parse_args(argv)

    rows = collect_rows()
    print(render_markdown(rows, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
