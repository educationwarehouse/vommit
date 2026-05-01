import tempfile
from contextlib import chdir
from pathlib import Path

from src.vommit import Config


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
        assert config_default.git.branch == "master"

        pyproject = Path(d) / "pyproject.toml"
        pyproject.write_text(contents)

        config = Config.from_pyproject()

        assert config.git.origin == "upstream"
        assert config.git.branch == "main"


# todo: test <!-- next-version-placeholder -->
#       test pluralize
