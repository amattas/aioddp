"""pyddp: asyncio DDP sender, color pipeline, and effects for LED pixel controllers."""

from importlib.metadata import PackageNotFoundError, version

from pyddp.color import kelvin_to_rgb, parse_color, process_frame
from pyddp.const import DDP_PORT, DEFAULT_FPS, DEFAULT_GAMMA, MAX_FPS, DataType
from pyddp.discovery import ControllerLayout, OutputInfo, discover
from pyddp.effects import EffectInfo, ParamSpec, get_effect, list_effects, validate_params
from pyddp.exceptions import ConfigError, ControllerError, DDPQueryError, DDPSendError, EffectError, PyDDPError
from pyddp.ndb import NDBController, NDBPreset, NDBState
from pyddp.runner import EffectRunner, RunnerStats, fade
from pyddp.sender import DDPSender, SenderStats
from pyddp.strip import Segment, Strip

try:
    __version__ = version("pyddp")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0"

__all__ = [
    "__version__",
    # models
    "Strip",
    "Segment",
    "DataType",
    # sending
    "DDPSender",
    "SenderStats",
    # discovery
    "ControllerLayout",
    "OutputInfo",
    "discover",
    # controller built-in effects (Minleon NDB-Pro)
    "NDBController",
    "NDBState",
    "NDBPreset",
    # effects
    "EffectRunner",
    "RunnerStats",
    "fade",
    "get_effect",
    "list_effects",
    "validate_params",
    "EffectInfo",
    "ParamSpec",
    # color
    "parse_color",
    "kelvin_to_rgb",
    "process_frame",
    # errors
    "PyDDPError",
    "ConfigError",
    "DDPSendError",
    "DDPQueryError",
    "ControllerError",
    "EffectError",
    # constants
    "DDP_PORT",
    "DEFAULT_FPS",
    "MAX_FPS",
    "DEFAULT_GAMMA",
]
