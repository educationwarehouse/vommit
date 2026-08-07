import tomllib
from importlib.metadata import version as dist_version
from pathlib import Path

import pytest

from src.vommit.__about__ import __version__
from src.vommit.helpers import canonical_version


def test_about_version_matches_pyproject_project_version() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project_version = data["project"]["version"]

    assert __version__ == project_version, "version mismatch - you should probably (re)run `uv pip install -e .`"
    assert dist_version("vommit") == project_version


def test_canonical_version_still_works_but_warns() -> None:
    """
    Deprecated rather than removed: it is exported, so someone may be calling it.
    """
    with pytest.warns(DeprecationWarning, match="canonical_version"):
        assert canonical_version("src.vommit") == dist_version("vommit")
