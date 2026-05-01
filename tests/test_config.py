import tempfile
from contextlib import chdir
from pathlib import Path

import pytest

from src.vommit import Config
from vommit.config import throw


def test_throw():
    with pytest.raises(ValueError):
        throw(ValueError(":)"))
        assert False

def test_config_pyproject():
    contents = """
    [tool.vommit]
    
    [tool.vommit.git]
    origin = 'upstream'
    branch = 'main'
    """

    with tempfile.TemporaryDirectory() as d, chdir(d):
        config_default = Config.from_pyproject()
        assert config_default == Config.default()

        assert config_default.git.origin == "origin"

        # fixme: properly init git; test find_main_branch_local and find_main_branch_upstream
        assert config_default.git.branch == "master"

        pyproject = Path(d) / "pyproject.toml"
        pyproject.write_text(contents)

        config = Config.from_pyproject()

        assert config.git.origin == "upstream"
        assert config.git.branch == "main"


# todo: test <!-- next-version-placeholder -->
#       test pluralize
