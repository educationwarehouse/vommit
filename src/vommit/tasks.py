# pragma: exclude file
#
# Entrypoints only: argument plumbing, output and error translation.
# Anything with logic in it belongs in a module that can be tested without a Context.

import shlex
import typing as t
from contextlib import contextmanager
from pathlib import Path

import rich
from ewok import Context, task
from invoke import Exit

from .bump import BumpRequest, BumpResult, run_bump, select_level
from .changelog import Changelog
from .config import DERIVE_BRANCH, Config
from .errors import VommitError
from .git import GitRepo
from .shell import ContextRunner

SetupMode = t.Literal["missing", "all"]


@contextmanager
def _reported() -> t.Iterator[None]:
    """
    Show library errors as a clean CLI failure instead of a traceback.
    """
    try:
        yield
    except VommitError as error:
        # Exit prints its message verbatim, so colour it here instead
        rich.print(f"[red]{error}[/red]")
        raise Exit(code=1) from error


def _notify(message: str) -> None:
    rich.print(f"[blue]{message}[/blue]")


def _project_root(project_dir: str | None) -> Path:
    return Path(project_dir) if project_dir else Path.cwd()


def _report(result: BumpResult) -> None:
    arrow = f"{result.previous} -> " if result.previous else ""
    suffix = " [dim](noop)[/dim]" if result.noop else ""
    rich.print(f"[green]Version {arrow}{result.version}[/green]{suffix}")

    if result.noop and result.entry:
        rich.print(f"\n[dim]{result.changelog_path}:[/dim]")
        rich.print(result.entry)
    if result.noop:
        return

    if result.commit_message:
        rich.print(f"[green]Committed[/green] {result.commit_message}")
    if result.tag:
        rich.print(f"[green]Tagged[/green] {result.tag}")


@task()
def init(c: Context, project_name: str, non_interactive: bool = False):
    c.run(f"uv init --package {shlex.quote(project_name)}")
    return setup(c, non_interactive=non_interactive, project_dir=project_name)


@task()
def setup(
    c: Context,
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
    root = _project_root(project_dir)
    pyproject = root / "pyproject.toml"

    with _reported():
        config = Config.from_pyproject(root)

        if not non_interactive:
            configured = Config.has_pyproject_config(pyproject)
            present_paths = (
                Config.configured_key_paths(pyproject)
                if mode == "missing" and configured
                else None
            )

            if mode == "missing" and Config.is_complete(pyproject):
                rich.print(
                    "[blue]Nothing to configure: existing vommit config is already complete.[/blue]"
                )
            else:
                config = Config.interactive(config, present_paths=present_paths)

        _pin_branch(c, config, root)
        config.write_to_pyproject(pyproject)
        if changelog := config.active_changelog:
            Changelog(settings=changelog, root=root).ensure()


def _pin_branch(c: Context, config: Config, root: Path) -> None:
    """
    Write the detected branch rather than `<head>`, so later runs are explicit.
    """
    if not (git := config.active_git) or git.branch != DERIVE_BRANCH:
        return
    repo = GitRepo(runner=ContextRunner(c), root=root)
    if branch := repo.main_branch(git.origin):
        git.branch = branch


@task()
def bump(
    c: Context,
    major: bool = False,
    minor: bool = False,
    patch: bool = False,
    prerelease: bool = False,
    noop: bool = False,
    version: str | None = None,
    allow_dirty: bool = False,
) -> str | None:
    """
    Bump the version, write the changelog, then commit and tag.

    Nothing is written until every check has passed, so a failed run leaves the
    project as it was. Use --noop to see the new version and changelog entry.
    """
    root = Path.cwd()

    with _reported():
        result = run_bump(
            config=Config.from_pyproject(root),
            runner=ContextRunner(c),
            root=root,
            request=BumpRequest(
                level=select_level(major, minor, patch),
                version=version,
                prerelease=prerelease,
                noop=noop,
                allow_dirty=allow_dirty,
            ),
            notify=_notify,
        )

    if result is None:
        rich.print("[yellow]No version-worthy changes found; nothing to bump.[/yellow]")
        return None

    _report(result)
    return result.version


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
    _config = Config.from_pyproject()


# todo: 'init' to make a whole new project? = uv init + setup
# todo: migrate from tool.semantic_release ; allow setting error/warn/skip on unexpected keys
# todo: `vommit add` to add a dependency?
