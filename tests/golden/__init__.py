"""Golden effect frames.

Each case is one effect at default params, ``seed`` 0, ``LENGTH`` pixels, at one of ``T_VALUES``. The frame is
stored as ``<effect>_<t>.bin`` with a ``.json`` sidecar recording the inputs. ``python -m tests.golden --write``
regenerates every file; ``--check`` (and the test suite) only compares. A changed golden is a reviewed artifact.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pyddp.effects import EFFECTS
from pyddp.effects.base import validate_params

GOLDEN_DIR = Path(__file__).parent
LENGTH = 300  # a typical roofline run; long enough that every effect's spatial pattern repeats
SEED = 0
T_VALUES = (0.0, 0.5, 123456.789)
CASES = [(name, t) for name in sorted(EFFECTS) for t in T_VALUES]


def _stem(name: str, t: float) -> Path:
    return GOLDEN_DIR / f"{name}_{t!r}"


def inputs(name: str, t: float) -> dict[str, Any]:
    """The JSON-normalized inputs for a case, as stored in its sidecar."""
    params = validate_params(EFFECTS[name], None)
    raw = {"effect": name, "length": LENGTH, "t": t, "seed": SEED, "params": params}
    normalized: dict[str, Any] = json.loads(json.dumps(raw))
    return normalized


def render_case(name: str, t: float) -> bytes:
    effect = EFFECTS[name]
    return effect().render(LENGTH, t, validate_params(effect, None), SEED)


def write_case(name: str, t: float) -> None:
    stem = _stem(name, t)
    stem.with_suffix(stem.suffix + ".bin").write_bytes(render_case(name, t))
    stem.with_suffix(stem.suffix + ".json").write_text(json.dumps(inputs(name, t), indent=2) + "\n")


def check_case(name: str, t: float) -> list[str]:
    """Problems with one case's golden files; empty when they match the current render."""
    stem = _stem(name, t)
    bin_path, json_path = stem.with_suffix(stem.suffix + ".bin"), stem.with_suffix(stem.suffix + ".json")
    if not bin_path.exists() or not json_path.exists():
        return [f"{stem.name}: golden files missing; run `python -m tests.golden --write`"]
    problems = []
    if json.loads(json_path.read_text()) != inputs(name, t):
        problems.append(f"{json_path.name}: recorded inputs differ from current defaults")
    if bin_path.read_bytes() != render_case(name, t):
        problems.append(f"{bin_path.name}: rendered frame differs from golden")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.golden", description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate every golden file")
    mode.add_argument("--check", action="store_true", help="compare current renders against golden files")
    args = parser.parse_args(argv)
    if args.write:
        for name, t in CASES:
            write_case(name, t)
        print(f"wrote {len(CASES)} golden frames to {GOLDEN_DIR}")
        return 0
    failed = 0
    for name, t in CASES:
        problems = check_case(name, t)
        failed += bool(problems)
        for problem in problems:
            print(problem)
    print(f"{len(CASES) - failed}/{len(CASES)} golden frames match")
    return 1 if failed else 0
