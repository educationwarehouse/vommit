from ewok import Context, task


@task()
def setup(
    _: Context,
    non_interactive: bool = False,
    name: str | None = None,
) -> None:
    """
    Init (default: interacitve ; allow --non-interactive with smart defaults):
    - check for vommit config in pyproject.toml
    - else: check for semantic-release < 8 config ; ask if user wants to migrate (tool.semantic_release; project.optional-dependencies.dev)
    - else: interactively ask to setup (tool.vommit ; project.optional-dependencies.dev)
    + Extra option for initializing totally new project?
    - uv init --package <name>
    """


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
