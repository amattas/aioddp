"""DDP wire constants, data types, and library defaults."""

from __future__ import annotations

from enum import Enum

DDP_PORT = 4048
HEADER_SIZE = 10
MAX_DATA_LEN = 1440  # DDP recommended max payload; 480 RGB or 360 RGBW pixels
MAX_OFFSET = 0xFFFF_FFFF  # 32-bit data offset
HEADER_FORMAT = ">BBBBIH"  # flags, sequence, data type, destination, offset, length

FLAG_VERSION_MASK = 0xC0
FLAG_VERSION_1 = 0x40
FLAG_TIMECODE = 0x10
FLAG_STORAGE = 0x08
FLAG_REPLY = 0x04
FLAG_QUERY = 0x02
FLAG_PUSH = 0x01

SEQUENCE_MIN = 1
SEQUENCE_MAX = 15  # low 4 bits; 0 means "sequence unused"

DESTINATION_DEFAULT = 1  # default output device
DESTINATION_CONTROL = 246  # JSON control read/write
DESTINATION_CONFIG = 250  # JSON config, read with a query
DESTINATION_STATUS = 251  # JSON status, read-only
DESTINATION_ALL = 255

DATA_TYPE_LEGACY = 0x01  # what older senders put in the data type byte

DEFAULT_FPS = 30
MAX_FPS = 60
DEFAULT_GAMMA = 2.2
DEFAULT_BRIGHTNESS_CAP = 1.0


class DataType(Enum):
    """Pixel layout: channel count and the DDP data type byte (``C R TTT SSS``)."""

    _value_: tuple[str, int, int]

    RGB = ("RGB", 3, 0x0B)  # type 001 (RGB), size 011 (8-bit)
    RGBW = ("RGBW", 4, 0x1B)  # type 011 (RGBW), size 011 (8-bit)

    @property
    def channels(self) -> int:
        """Bytes per pixel."""
        return self.value[1]

    @property
    def wire_byte(self) -> int:
        """Data type byte sent in the DDP header."""
        return self.value[2]
