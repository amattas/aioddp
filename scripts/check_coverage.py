"""Enforce per-file line-coverage floors from coverage.json (written by pytest via addopts)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

FLOORS: dict[str, float] = {
    "src/pyddp/protocol.py": 95.0,
    "src/pyddp/color.py": 95.0,
    "src/pyddp/runner.py": 90.0,
    "src/pyddp/cli.py": 90.0,
    "src/pyddp/discovery.py": 90.0,
    "src/pyddp/ndb.py": 90.0,
}


def main(report_path: str = "coverage.json") -> int:
    """Print one line per gated file; return 1 if any is missing or below its floor."""
    files = json.loads(Path(report_path).read_text())["files"]
    failed = False
    for path, floor in FLOORS.items():
        summary = files.get(path, {}).get("summary")
        if summary is None:
            print(f"FAIL {path}: not in {report_path}")
            failed = True
            continue
        statements = summary["num_statements"]
        percent = 100.0 * summary["covered_lines"] / statements if statements else 100.0
        ok = percent >= floor
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {path}: {percent:.1f}% lines (floor {floor:.0f}%)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
