import typing as t
import warnings
from importlib.metadata import version
from pathlib import Path

import tomlkit


def read_toml(path: Path) -> tomlkit.TOMLDocument:
    """
    A TOML file as a document that remembers its comments and formatting.

    Every write path re-parses rather than caching: the file is edited in
    several passes, and a stale document would silently undo an earlier one.
    """
    return tomlkit.parse(path.read_text())


def throw(error: Exception) -> t.Never:
    """
    Functional raise, useful for if ... else ... or callbacks.
    """
    raise error


DEPRECATED_CANONICAL_VERSION = (
    "canonical_version() is deprecated: it derives a distribution name from an "
    "import name, and the two only agree by accident: `src.my_lib` never "
    "reduces to the distribution `my-lib`, so the lookup raises "
    "PackageNotFoundError at import time. Name the distribution instead: "
    '`__version__ = version("my-lib")`. `vommit setup` rewrites the file for you.'
)


def canonical_version(package_name: str):
    """
    Deprecated: the version of a package, by the last segment of its import
    name. Superseded by naming the distribution outright. See
    `DEPRECATED_CANONICAL_VERSION`.

    Args:
        package_name (str): The name of the package. Can be a fully qualified
        package name containing dots.

    Returns:
        str: The version of the specified package.
    """
    warnings.warn(DEPRECATED_CANONICAL_VERSION, DeprecationWarning, stacklevel=2)

    # e.g. src.vommit -> vommit
    package = package_name.split(".")[-1] if "." in package_name else package_name
    return version(package)


def relative_path(path: Path, root: Path) -> str:
    """
    `path` as written from `root`, or in full when it lies outside it.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
