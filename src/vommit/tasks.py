# pragma: exclude file
#
# Entrypoints only: argument plumbing, output and error translation.
# Anything with logic in it belongs in a module that can be tested without a Context.

import shlex
import sys
import typing as t
from contextlib import contextmanager
from pathlib import Path

import questionary
import rich
from ewok import Context, task
from invoke import Exit
from rich.markup import escape

from .bump import BumpRequest, BumpResult, always, run_bump, select_level
from .changelog import Changelog
from .config import DERIVE_BRANCH, TOML_KEY, Config
from .errors import VommitError
from .git import GitRepo
from .helpers import relative_path
from .migrate import (
    PSR_KEY,
    Migration,
    MigrationReport,
    VersionFile,
    apply_static_version,
    plan_static_version,
    read_psr,
    rewrite_version_files,
    strip_psr,
    translate,
)
from .shell import ContextRunner
from .undo import UndoPlan, UndoResult, run_undo

SetupMode = t.Literal["missing", "all"]
UnsupportedPolicy = t.Literal["warn", "error", "skip"]
MigrateChoice = t.Literal["interactive", "copy", "stop"]

MIGRATE_CHOICES: dict[MigrateChoice, str] = {
    "interactive": "interactive - walk through every setting, pre-filled from v7",
    "copy": "copy - write the translated config as it stands",
    "stop": "stop - write nothing (the report above was the dry run)",
}


@contextmanager
def _reported() -> t.Iterator[None]:
    """
    Show library errors as a clean CLI failure instead of a traceback.
    """
    try:
        yield
    except VommitError as error:
        # Exit prints its message verbatim, so colour it here instead
        rich.print(f"[red]{escape(str(error))}[/red]")
        raise Exit(code=1) from error


def _notify(message: str) -> None:
    rich.print(f"[blue]{message}[/blue]")


def _project_root(project_dir: str | None) -> Path:
    return Path(project_dir) if project_dir else Path.cwd()


def _report(result: BumpResult) -> None:
    if result.cancelled:
        rich.print("[yellow]Cancelled; nothing was written.[/yellow]")
        return

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


def _report_undo(result: UndoResult) -> None:
    if result.cancelled:
        rich.print("[yellow]Cancelled; nothing was undone.[/yellow]")
        return

    plan = result.plan
    suffix = " [dim](noop)[/dim]" if result.noop else ""
    rich.print(f"[green]Version {plan.version} -> {plan.previous}[/green]{suffix}")
    for line in _undo_steps(plan):
        rich.print(f"  [dim]-[/dim] {line}")


def _undo_steps(plan: UndoPlan) -> list[str]:
    """
    What undoing this release touches, in the order it happens.
    """
    steps = [f"delete tag {plan.tag}"] if plan.tag else []
    if plan.rewinds_history:
        return [*steps, f"drop the release commit ({plan.commit})"]

    steps.append(f"set the version back to {plan.previous}")
    if plan.changelog_path:
        steps.append(f"remove the {plan.version} entry from {plan.changelog_path.name}")
    return steps


def _bump_question(result: BumpResult) -> str:
    if result.entry:
        rich.print(f"\n[dim]{result.changelog_path}:[/dim]")
        rich.print(result.entry)
        rich.print("")

    arrow = f"{result.previous} -> " if result.previous else ""
    extras = [
        label
        for label, value in (("commit", result.commit_message), ("tag", result.tag))
        if value
    ]
    tail = f" and {' and '.join(extras)} it" if extras else ""
    return f"Release {arrow}{result.version}{tail}?"


def _undo_question(result: UndoResult) -> str:
    for line in _undo_steps(result.plan):
        rich.print(f"  [dim]-[/dim] {line}")
    return f"Undo {result.plan.version}, back to {result.plan.previous}?"


def _asker[ResultT](
    config: Config,
    yes: bool,
    question: t.Callable[[ResultT], str],
) -> t.Callable[[ResultT], bool]:
    """
    The confirmation step, or a straight yes when nobody has to be asked.

    A missing terminal is a no rather than a yes: an unattended run that wanted
    to go ahead would have passed --yes, and stopping beats releasing by default.
    """
    if yes or not config.confirm:
        return always

    def ask(result: ResultT) -> bool:
        text = question(result)
        if not sys.stdin.isatty():
            rich.print(f"[yellow]{text} No terminal to ask on; pass --yes.[/yellow]")
            return False
        return bool(questionary.confirm(text, default=True).ask())

    return ask


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
        if _offers_migration(pyproject, non_interactive):
            return migrate(c, project_dir=project_dir)

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

        _write_config(c, config, root, pyproject)


def _write_config(c: Context, config: Config, root: Path, pyproject: Path) -> None:
    """
    The tail every setup path shares: pin the branch, write, make a changelog.
    """
    _pin_branch(c, config, root)
    config.write_to_pyproject(pyproject)
    if changelog := config.active_changelog:
        Changelog(settings=changelog, root=root).ensure()


@task()
def migrate(
    c: Context,
    project_dir: str | None = None,
    on_unsupported: UnsupportedPolicy = "warn",
    yes: bool = False,
) -> None:
    """
    Bring a python-semantic-release v7 project across to vommit.

    Reads `[tool.semantic_release]`, reports what does and does not survive the
    translation, then asks whether to review the result, take it as-is, or stop.
    Nothing is written before that answer, so a plain run doubles as a dry run.
    Use --on-unsupported=error to refuse a config vommit cannot fully honour.
    """
    root = _project_root(project_dir)
    pyproject = root / "pyproject.toml"

    with _reported():
        raw = read_psr(pyproject)
        if raw is None:
            raise VommitError(
                f"no [{PSR_KEY}] in {pyproject}; there is nothing to migrate. "
                f"Run `vommit setup` to configure from scratch."
            )

        migration = translate(raw)
        _report_migration(migration.report, on_unsupported)

        config = _migrated_config(migration, yes)
        if config is None:
            rich.print("[yellow]Stopped; nothing was written.[/yellow]")
            return

        _write_config(c, config, root, pyproject)
        rich.print(f"[green]Wrote[/green] {escape(f'[{TOML_KEY}]')} to {pyproject}")

        _migrate_version(c, config, root, pyproject, migration.version_files, yes)
        _migrate_cleanup(pyproject, yes)


def _report_migration(report: MigrationReport, policy: UnsupportedPolicy) -> None:
    for note in report.mapped:
        rich.print(
            f"  [green]+[/green] {escape(note.key)} [dim]->[/dim] {escape(note.detail)}"
        )
    for note in report.lossy:
        rich.print(f"  [yellow]~[/yellow] {escape(note.key)}: {escape(note.detail)}")

    if policy == "skip":
        return

    for note in report.unsupported:
        rich.print(f"  [red]-[/red] {escape(note.key)}: {escape(note.detail)}")

    if policy == "error" and report.unsupported:
        raise VommitError(
            f"{len(report.unsupported)} setting(s) have no vommit equivalent; "
            f"--on-unsupported=error refuses to continue."
        )


def _migrated_config(migration: Migration, yes: bool) -> Config | None:
    """
    The config to write, or None when the answer was to write nothing.
    """
    choice = _migrate_choice(migration.report, yes)
    if choice == "stop":
        return None
    if choice == "copy":
        return migration.config
    return Config.interactive(migration.config, present_paths=migration.key_paths)


def _migrate_choice(report: MigrationReport, yes: bool) -> MigrateChoice:
    """
    Ask what to do with the translation; warnings tilt the default to review.
    """
    if yes:
        return "copy"
    if not sys.stdin.isatty():
        rich.print(
            "[yellow]No terminal to ask on; pass --yes to write the translated "
            "config.[/yellow]"
        )
        return "stop"

    default: MigrateChoice = "copy" if report.clean else "interactive"
    question = (
        "Everything came across. Write it?"
        if report.clean
        else f"{len(report.warnings)} setting(s) need a look. How do you want to go on?"
    )
    answer = questionary.select(
        question,
        choices=list(MIGRATE_CHOICES.values()),
        default=MIGRATE_CHOICES[default],
    ).ask()
    return next(
        (choice for choice, label in MIGRATE_CHOICES.items() if label == answer),
        "stop",
    )


def _migrate_version(
    c: Context,
    config: Config,
    root: Path,
    pyproject: Path,
    version_files: list[VersionFile],
    yes: bool,
) -> None:
    """
    Make the version bumpable: static in `[project]`, derived in the source.

    The two halves are one change. Rewriting `__version__` while the backend
    still reads its version from that file leaves the package unbuildable.
    """
    transform = plan_static_version(
        pyproject, version_files, fallback=_last_released_version(c, config, root)
    )
    if transform is None and not version_files:
        return

    steps = list(transform.steps) if transform else []
    steps += [
        f"rewrite {version_file.path} to "
        f"`{version_file.variable} = version(__package__)`"
        for version_file in version_files
    ]
    for step in steps:
        rich.print(f"  [dim]-[/dim] {escape(step)}")

    question = (
        "This project declares a dynamic version, which cannot be bumped. Fix it?"
        if transform
        else "Point the version file at the installed metadata?"
    )
    if not _confirm(question, yes):
        rich.print("[yellow]Left the version alone.[/yellow]")
        return

    if transform:
        apply_static_version(pyproject, transform)
    result = rewrite_version_files(version_files, root)

    for path in result.changed:
        rich.print(f"[green]Rewrote[/green] {escape(relative_path(path, root))}")
    for note in result.skipped:
        rich.print(
            f"[yellow]Skipped {escape(note.key)}: {escape(note.detail)}[/yellow]"
        )


def _last_released_version(c: Context, config: Config, root: Path) -> str | None:
    """
    The version behind the most recent tag, as a fallback version source.
    """
    git = config.git
    repo = GitRepo(runner=ContextRunner(c), root=root)
    return config.tag_version(repo.last_tag(git.tag_glob if git else None))


def _migrate_cleanup(pyproject: Path, yes: bool) -> None:
    """
    Two live release configs in one file is the state worth not leaving behind.
    """
    question = f"Remove [{PSR_KEY}] and the python-semantic-release dependency?"
    if not _confirm(question, yes):
        rich.print(f"[yellow]Left {escape(f'[{PSR_KEY}]')} in place.[/yellow]")
        return

    for removed in strip_psr(pyproject):
        rich.print(f"[green]Removed[/green] {escape(removed)}")


def _offers_migration(pyproject: Path, non_interactive: bool) -> bool:
    """
    Whether `setup` should hand over to `migrate` instead of starting blank.
    """
    if Config.has_pyproject_config(pyproject) or read_psr(pyproject) is None:
        return False

    rich.print(
        f"[blue]Found a python-semantic-release config in {escape(str(pyproject))}.[/blue]"
    )
    if non_interactive:
        rich.print("[blue]Run `vommit migrate` to bring it across.[/blue]")
        return False
    return _confirm("Migrate it instead of configuring from scratch?", yes=False)


def _confirm(question: str, yes: bool, default: bool = True) -> bool:
    """
    Ask, unless there is nobody to ask or the answer was given up front.
    """
    if yes:
        return True
    if not sys.stdin.isatty():
        rich.print(f"[yellow]{question} No terminal to ask on; skipped.[/yellow]")
        return False
    return bool(questionary.confirm(question, default=default).ask())


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
    undo: bool = False,
    yes: bool = False,
) -> str | None:
    """
    Bump the version, write the changelog, then commit and tag.

    Nothing is written until every check has passed, so a failed run leaves the
    project as it was. Use --noop to see the new version and changelog entry,
    --yes to skip the confirmation, and --undo to take the last release back.
    """
    root = Path.cwd()
    config = Config.from_pyproject(root)

    if undo:
        return _undo(
            c,
            config,
            root,
            request=lambda: BumpRequest(
                level=select_level(major, minor, patch),
                version=version,
                prerelease=prerelease,
                allow_dirty=allow_dirty,
                undo=True,
            ),
            noop=noop,
            yes=yes,
        )

    with _reported():
        result = run_bump(
            config=config,
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
            confirm=_asker(config, yes, _bump_question),
        )

    if result is None:
        rich.print("[yellow]No version-worthy changes found; nothing to bump.[/yellow]")
        return None

    _report(result)
    if result.cancelled:
        raise Exit(code=1)
    return result.version


def _undo(
    c: Context,
    config: Config,
    root: Path,
    request: t.Callable[[], BumpRequest],
    noop: bool,
    yes: bool,
) -> str | None:
    with _reported():
        # built only to have it reject the flags that make no sense here
        request()
        result = run_undo(
            config=config,
            runner=ContextRunner(c),
            root=root,
            noop=noop,
            notify=_notify,
            confirm=_asker(config, yes, _undo_question),
        )

    _report_undo(result)
    if result.cancelled:
        raise Exit(code=1)
    return result.plan.previous


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
# todo: `vommit add` to add a dependency?
