import typing as t
from importlib.metadata import version
from pathlib import Path


def throw(error: Exception) -> t.Never:
    """
    Functional raise, useful for if ... else ... or callbacks.
    """
    raise error


def canonical_version(package_name: str):
    """
    Determines the canonical version of a package.

    This function extracts the last segment of a package name if it includes dots,
    otherwise it uses the full package name. It then retrieves the corresponding
    version of the package.

    Args:
        package_name (str): The name of the package. Can be a fully qualified
        package name containing dots.

    Returns:
        str: The version of the specified package.

    Example:
        __version__ = vommit.canonical_version(__package__)
    """

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
