"""Replay every fuzz seed and saved crash input through its atheris target's ``test_one_input``.

Each target asserts its decoder's documented exception contract, so this runs the same oracle on every PR without
atheris. The corpus directory name is the target module name: ``fuzz/corpus/<target>/<input>``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from functools import cache
from pathlib import Path

import pytest

FUZZ_DIR = Path(__file__).resolve().parent.parent / "fuzz"
CORPUS = sorted(path for path in (FUZZ_DIR / "corpus").glob("*/*") if path.is_file())


@cache
def _target(name: str) -> Callable[[bytes], None]:
    spec = importlib.util.spec_from_file_location(name, FUZZ_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"no fuzz target for corpus directory {name}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target: Callable[[bytes], None] = module.test_one_input
    return target


@pytest.mark.parametrize("path", CORPUS, ids=[f"{p.parent.name}/{p.name}" for p in CORPUS])
def test_corpus_input_honours_contract(path: Path) -> None:
    _target(path.parent.name)(path.read_bytes())
