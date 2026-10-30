"""pyddp exception hierarchy."""


class PyDDPError(Exception):
    """Base class for every pyddp error."""


class ConfigError(PyDDPError, ValueError):
    """Invalid strip, segment, params, or color."""


class DDPSendError(PyDDPError):
    """The sender cannot send at all: socket could not be opened or the sender is closed."""


class DDPQueryError(PyDDPError):
    """A controller answered a query or config request with something unusable: bad JSON, not an object, bad ports."""


class EffectError(PyDDPError):
    """Unknown effect, bad effect params, or a render failure."""


class ControllerError(PyDDPError):
    """A controller's HTTP control interface could not be reached, timed out, or answered with a non-200 status."""
