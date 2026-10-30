"""aioddp exception hierarchy."""


class DDPError(Exception):
    """Base class for every aioddp error."""


class ConfigError(DDPError, ValueError):
    """Invalid strip, segment, params, or color."""


class DDPSendError(DDPError):
    """The sender cannot send at all: socket could not be opened or the sender is closed."""


class DDPQueryError(DDPError):
    """A controller answered a query or config request with something unusable: bad JSON, not an object, bad ports."""


class EffectError(DDPError):
    """Unknown effect, bad effect params, or a render failure."""
