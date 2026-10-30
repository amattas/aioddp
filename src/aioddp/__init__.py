"""aioddp: asyncio DDP sender, color pipeline, and effects for LED pixel controllers."""

from importlib.metadata import PackageNotFoundError, version

from aioddp.color import kelvin_to_rgb, parse_color, process_frame
from aioddp.const import DDP_PORT, DEFAULT_FPS, DEFAULT_GAMMA, MAX_FPS, DataType
from aioddp.discovery import ControllerLayout, OutputInfo, discover, parse_config
from aioddp.effects import EffectInfo, ParamSpec, get_effect, list_effects, validate_params
from aioddp.exceptions import ConfigError, DDPError, DDPQueryError, DDPSendError, EffectError
from aioddp.runner import EffectRunner, RunnerStats, fade
from aioddp.sender import DDPSender, SenderStats
from aioddp.strip import Segment, Strip

try:
    __version__ = version("aioddp")
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
    "parse_config",
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
    "DDPError",
    "ConfigError",
    "DDPSendError",
    "DDPQueryError",
    "EffectError",
    # constants
    "DDP_PORT",
    "DEFAULT_FPS",
    "MAX_FPS",
    "DEFAULT_GAMMA",
]
