import tomllib
from importlib.metadata import version as dist_version
from pathlib import Path

from src.vommit.__about__ import __version__


def test_about_version_matches_pyproject_project_version() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project_version = data["project"]["version"]

    assert __version__ == project_version
    assert dist_version("vommit") == project_version
