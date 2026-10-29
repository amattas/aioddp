"""pyddp exception hierarchy."""


class PyDDPError(Exception):
    """Base class for every pyddp error."""


class ConfigError(PyDDPError, ValueError):
    """Invalid strip, segment, params, or color."""


class DDPSendError(PyDDPError):
    """The sender cannot send at all: socket could not be opened or the sender is closed."""


class EffectError(PyDDPError):
    """Unknown effect, bad effect params, or a render failure."""
