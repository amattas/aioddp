"""Public package surface: exports, version, and import hygiene."""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import version

import pyddp

SPEC_EXPORTS = {
    "__version__",
    *("Strip", "Segment", "DataType"),
    *("DDPSender", "SenderStats"),
    *("ControllerLayout", "OutputInfo", "discover"),
    *("EffectRunner", "RunnerStats", "fade", "get_effect", "list_effects", "validate_params"),
    *("EffectInfo", "ParamSpec"),
    *("parse_color", "kelvin_to_rgb", "process_frame"),
    *("PyDDPError", "ConfigError", "DDPSendError", "DDPQueryError", "EffectError"),
    *("DDP_PORT", "DEFAULT_FPS", "MAX_FPS", "DEFAULT_GAMMA"),
}


def test_all_matches_spec_and_resolves() -> None:
    assert set(pyddp.__all__) == SPEC_EXPORTS
    assert len(pyddp.__all__) == len(SPEC_EXPORTS)
    assert all(hasattr(pyddp, name) for name in pyddp.__all__)
    assert pyddp.__version__ == version("pyddp")


def test_import_does_not_pull_in_cli_dependencies() -> None:
    probe = (
        "import sys, pyddp; "
        "print(','.join(sorted(m for m in ('click', 'rich', 'yaml', 'pyddp.cli') if m in sys.modules)))"
    )
    argv = [sys.executable, "-I", "-c", probe]  # fixed argv, no untrusted input
    result = subprocess.run(argv, capture_output=True, text=True, check=True)  # noqa: S603
    assert result.stdout.strip() == ""
