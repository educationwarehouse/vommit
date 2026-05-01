import tempfile
import textwrap
import uuid
from contextlib import chdir
from pathlib import Path

import pytest

from src.vommit.config import (
    ChangelogConfig,
    Config,
    bash,
    find_main_branch_local,
    find_main_branch_upstream,
    throw,
)


def test_throw():
    with pytest.raises(ValueError):
        throw(ValueError(":)"))
        assert False


def test_config_pyproject():
    contents = """
    [tool.vommit]
    
    [tool.vommit.git]
    origin = 'upstream'
    branch = 'myproject/staging'
    """

    branch_identifier = str(uuid.uuid4())

    with tempfile.TemporaryDirectory() as d, chdir(d):
        bash(f"git init --initial-branch=branch-local-{branch_identifier}")
        bash(f"git config init.defaultBranch branch-local-{branch_identifier}")
        config_default = Config.from_pyproject()
        assert config_default == Config.default()

        assert config_default.git.origin == "origin"
        assert config_default.git.branch == f"branch-local-{branch_identifier}"

        pyproject = Path(d) / "pyproject.toml"
        pyproject.write_text(contents)

        config = Config.from_pyproject()

        assert config.git.origin == "upstream"
        assert config.git.branch == "myproject/staging"


def _init_repo(path: Path, branch: str) -> None:
    with chdir(path):
        bash(f"git init --initial-branch={branch}")
        bash(f"git config init.defaultBranch {branch}")
        bash("git config user.name test")
        bash("git config user.email test@example.com")
    (path / "README.md").write_text("init\n")
    with chdir(path):
        bash("git add README.md")
        bash("git commit -m init")


def test_find_main_branch_local():
    branch_identifier = str(uuid.uuid4())

    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        repo.mkdir()
        _init_repo(repo, f"branch-local-{branch_identifier}")

        with chdir(repo):
            assert find_main_branch_local() == f"branch-local-{branch_identifier}"


def test_find_main_branch_upstream():
    branch_identifier = str(uuid.uuid4())

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        remote = root / "remote.git"
        local = root / "local"
        remote.mkdir()
        local.mkdir()

        with chdir(remote):
            bash(f"git init --bare --initial-branch=branch-remote-{branch_identifier}")

        _init_repo(local, f"branch-remote-{branch_identifier}")

        with chdir(local):
            bash(f"git remote add origin {remote}")
            bash(f"git push -u origin branch-remote-{branch_identifier}")

        with chdir(local):
            assert (
                find_main_branch_upstream("origin")
                == f"branch-remote-{branch_identifier}"
            )


def test_placeholder_marker_regex():
    some_changelog = textwrap.dedent("""
    # Changelog
    read this for the latest changes; new changes should be inserted at `<!-- latest-version-placeholder -->` below.
    
    <!-- latest-version-placeholder -->
    
    ### Feature
    * Introduced `<!-- latest-version-placeholder -->`
    """)

    config = ChangelogConfig.load(
        {
            "placeholder_regex": r"^<!--\s*latest-version-placeholder\s*-->$",
        }
    )

    to_insert = textwrap.dedent("""
    ### Fix
    * New Version :)
    """)

    expected = textwrap.dedent("""
    # Changelog
    read this for the latest changes; new changes should be inserted at `<!-- latest-version-placeholder -->` below.
    
    <!-- latest-version-placeholder -->
    
    ### Fix
    * New Version :)
    
    ### Feature
    * Introduced `<!-- latest-version-placeholder -->`
    """)

    assert config.apply_placeholder("nothing in here", to_insert) is None

    assert config.apply_placeholder(some_changelog, to_insert) == expected


def test_pluralize_templates():
    config = ChangelogConfig.default()

    assert config.pluralize("feat", 1) == "Feature"
    assert config.pluralize("feat", 2) == "Features"

    assert config.pluralize("fix", 1) == "Bug Fix"
    assert config.pluralize("fix", 2) == "Bug Fixes"

    assert config.pluralize("perf", 1) == "Performance"
    assert config.pluralize("docs", 2) == "Documentation"
