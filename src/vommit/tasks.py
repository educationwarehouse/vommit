# pragma: exclude file

import shlex
import typing as t

import rich
from ewok import Context, task

from .config import Config

SetupMode = t.Literal["missing", "all"]


@task()
def init(c: Context, project_name: str, non_interactive: bool = False):
    c.run(f"uv init --package {shlex.quote(project_name)}")
    return setup(c, non_interactive=non_interactive, project_dir=project_name)


@task()
def setup(
    _: Context,
    non_interactive: bool = False,
    project_dir: str | None = None,
    mode: SetupMode = "missing",
) -> None:
    """
    Init (default: interacitve ; allow --non-interactive with smart defaults):
    - check for vommit config in pyproject.toml
    - else: check for semantic-release < 8 config ; ask if user wants to migrate (tool.semantic_release; project.optional-dependencies.dev)
    - else: interactively ask to setup (tool.vommit ; project.optional-dependencies.dev)
    + ask if user wants to switch __about__ to `__version__ = version(__package__)`
    + Extra option for initializing totally new project?
    - uv init --package <name>
    """
    config = Config.from_pyproject()

    if not non_interactive:
        present_paths = (
            Config.configured_key_paths()
            if mode == "missing" and Config.has_pyproject_config()
            else None
        )

        if mode == "missing" and Config.is_complete():
            rich.print(
                "[blue]Nothing to configure: existing vommit config is already complete.[/blue]"
            )
            config.write_to_pyproject()
            config.changelog.ensure_file(project_dir)
            return

        config = Config.interactive(config, present_paths=present_paths)

    config.write_to_pyproject()
    config.changelog.ensure_file(project_dir)


@task()
def bump(
    _: Context,
    major: bool = False,
    minor: bool = False,
    patch: bool = False,
    prerelease: bool = False,
    noop: bool = False,
    version: str | None = None,
) -> None:
    """
    Bump:
    - ensure no upstream changes (without pulling)
    - uv version bump
    - Write changelog
    - commit + tag
    """
    config = Config.from_pyproject()


@task()
def release(
    _: Context,
    major: bool = False,
    minor: bool = False,
    patch: bool = False,
) -> None:
    """
    Release:
    - git pull
    - bump
    - git push
    - (clean dist)
    - uv build
    - uv publish
    """
    config = Config.from_pyproject()


# todo: 'init' to make a whole new project? = uv init + setup
# todo: migrate from tool.semantic_release ; allow setting error/warn/skip on unexpected keys
# todo: `vommit add` to add a dependency?
