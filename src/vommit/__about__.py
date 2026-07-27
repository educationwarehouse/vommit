from .helpers import canonical_version

assert __package__, "unknown package?"

__version__ = canonical_version(__package__)
