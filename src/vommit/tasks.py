# pragma: exclude file

import shlex

from ewok import Context, task

from .config import Config

# def task(*a, **kw):
#     return ewok_task(*a, **kw)


@task()
def init(c: Context, project_name: str, non_interactive: bool = False):
    c.run(f"uv init --package {shlex.quote(project_name)}")
    return setup(c, non_interactive=non_interactive)


@task()
def setup(
    _: Context,
    non_interactive: bool = False,
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
    current_config = Config.from_pyproject()

    config = current_config if non_interactive else Config.interactive(current_config)


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
