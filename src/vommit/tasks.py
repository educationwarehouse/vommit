# pragma: exclude file
#
# Entrypoints only: argument plumbing, output and error translation.
# Anything with logic in it belongs in a module that can be tested without a Context.

import sys
import typing as t
from contextlib import contextmanager
from pathlib import Path

import questionary
import rich
from ewok import Context, task
from invoke import Exit
from rich.markup import escape

from . import licenses
from .auth import (
    ENVIRONMENT,
    KEYRING,
    PROMPT,
    TOKEN_VAR,
    TokenStore,
    environment_token,
    mask,
    verify_token,
)
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
    UnsupportedPolicy,
    VersionPlan,
    apply_static_version,
    parse_unsupported_policy,
    plan_version_migration,
    read_psr,
    rewrite_version_files,
    strip_psr,
    translate,
)
from .interactive import Prompts
from .release import ReleaseRequest, ReleaseResult, Step, run_release
from .scaffold import (
    DEFAULT_BRANCH,
    DEFAULT_COMMIT_MESSAGE,
    Defaults,
    ScaffoldRequest,
    ScaffoldResult,
    plan_request,
    run_scaffold,
)
from .shell import ContextRunner
from .undo import UndoPlan, UndoResult, run_undo

SetupMode = t.Literal["missing", "all"]
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


def _report_release(result: ReleaseResult) -> None:
    if result.cancelled:
        rich.print("[yellow]Cancelled; nothing was written.[/yellow]")
        return

    if result.bump:
        _report(result.bump)
    elif result.noop:
        rich.print(f"[green]Version {result.version}[/green] [dim](noop)[/dim]")

    if result.noop:
        _report_planned(result.steps)
        return

    if result.pushed:
        rich.print("[green]Pushed[/green]")
    if result.published:
        rich.print(f"[green]Published[/green] {result.version}")


def _report_planned(steps: tuple[Step, ...]) -> None:
    if not steps:
        return
    rich.print("\n[dim]would run:[/dim]")
    for step in steps:
        rich.print(f"  [dim]{step.name}:[/dim] {escape(step.command)}")


@contextmanager
def _step(name: str) -> t.Iterator[None]:
    """
    Show that a command is running; its output only surfaces when it fails.
    """
    with rich.get_console().status(f"[blue]{escape(name)}[/blue]"):
        yield
    rich.print(f"[green]{name}[/green]")


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


@task(
    # long flags only: a dozen parameters here left invoke deriving short flags
    # like `-y` for `--python`, and one collision produced a bare `--`
    auto_shortflags=False,
    help={
        "project-name": "Directory and package name to create.",
        "non-interactive": "Take every answer from the flags and defaults.",
        "python": "Minimum Python version (default: the interpreter running vommit).",
        "description": "Project description; omitted from pyproject.toml when empty.",
        "license": f"SPDX identifier, or '{licenses.NO_LICENSE}' (default: MIT).",
        "branch": "Release branch (default: the one git creates).",
        "remote": "URL to add as 'origin'.",
        "message": "Initial commit message; empty makes no commit.",
        "push": "Push the initial commit and set the upstream.",
        "no-sync": "Skip `uv sync`, leaving no .venv or uv.lock.",
        "pin-python": "Keep uv's .python-version file.",
        "no-workspace": "Do not join an enclosing uv workspace.",
    },
)
def init(
    c: Context,
    project_name: str,
    non_interactive: bool = False,
    python: str | None = None,
    description: str | None = None,
    license: str | None = None,
    branch: str | None = None,
    remote: str | None = None,
    message: str | None = None,
    push: bool = False,
    no_sync: bool = False,
    pin_python: bool = False,
    no_workspace: bool = False,
) -> None:
    """
    Create a new uv package, configure vommit in it, and make its first commit.

    Asks for the Python floor, description, license, release branch, remote and
    commit message; every flag here pre-fills its question, and
    `--non-interactive` takes the answers as given.
    """
    runner = ContextRunner(c)
    with _reported():
        defaults = ScaffoldRequest(
            project_name=project_name,
            python=python,
            description=description,
            license_id=license or licenses.DEFAULT_LICENSE,
            branch=branch or DEFAULT_BRANCH,
            pin_python=pin_python,
            workspace=not no_workspace,
            remote=remote,
            commit_message=message or DEFAULT_COMMIT_MESSAGE,
            push=push,
            sync=not no_sync,
        )
        asker = Defaults() if non_interactive else Prompts()
        cwd = Path.cwd()
        request = plan_request(
            defaults,
            asker,
            detected_branch=branch or _detected_branch(runner, cwd),
        )
        result = run_scaffold(
            request,
            runner=runner,
            cwd=cwd,
            configure=lambda root: _configure_new(c, root, non_interactive),
            notify=_notify,
        )
        _report_scaffold(result)


def _detected_branch(runner: ContextRunner, cwd: Path) -> str:
    """
    The branch name a `git init` here would produce, for the prompt's default.
    """
    return GitRepo(runner=runner, root=cwd).main_branch_local() or DEFAULT_BRANCH


def _configure_new(c: Context, root: Path, non_interactive: bool) -> None:
    """
    Write the vommit config into the project `init` just created.
    """
    setup(c, non_interactive=non_interactive, project_dir=str(root))


def _report_scaffold(result: ScaffoldResult) -> None:
    rich.print(f"[green]Created[/green] {escape(str(result.root))}")
    rich.print(f"  [dim]branch[/dim]  {escape(result.branch)}")
    if result.license_id:
        rich.print(f"  [dim]license[/dim] {escape(result.license_id)}")
    if result.remote:
        rich.print(f"  [dim]remote[/dim]  {escape(result.remote)}")
    if not result.committed:
        rich.print("[yellow]No initial commit was made.[/yellow]")
    if result.remote and not result.pushed:
        rich.print("[blue]Nothing was pushed yet.[/blue]")


@task()
def setup(
    c: Context,
    non_interactive: bool = False,
    project_dir: str | None = None,
    mode: SetupMode = "missing",
) -> None:
    """
    Create or complete the vommit config in this project's pyproject.toml.

    Hands over to `migrate` when it finds a python-semantic-release v7 config
    and nothing of vommit's own. `--mode=all` revisits every setting rather than
    only the missing ones.
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
    on_unsupported: str = "warn",
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
        policy = parse_unsupported_policy(on_unsupported)

        raw = read_psr(pyproject)
        if raw is None:
            raise VommitError(
                f"no [{PSR_KEY}] in {pyproject}; there is nothing to migrate. "
                f"Run `vommit setup` to configure from scratch."
            )

        migration = translate(raw)
        _report_migration(migration.report, policy)

        # planned before the first write, so a project this cannot migrate is
        # refused whole rather than left with two live release configs
        plan = plan_version_migration(
            pyproject,
            migration.version_files,
            fallback=_last_released_version(c, migration.config, root),
        )

        config = _migrated_config(migration, yes)
        if config is None:
            rich.print("[yellow]Stopped; nothing was written.[/yellow]")
            return

        _write_config(c, config, root, pyproject)
        rich.print(f"[green]Wrote[/green] {escape(f'[{TOML_KEY}]')} to {pyproject}")

        _migrate_version(plan, root, pyproject, yes)
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
    plan: VersionPlan,
    root: Path,
    pyproject: Path,
    yes: bool,
) -> None:
    """
    Carry out the version half of the migration, once it has been agreed to.
    """
    if plan.empty:
        return

    for step in plan.steps:
        rich.print(f"  [dim]-[/dim] {escape(step)}")
    for note in plan.warnings:
        rich.print(f"  [yellow]![/yellow] {escape(note.key)}: {escape(note.detail)}")

    if not _confirm(plan.question, yes):
        rich.print("[yellow]Left the version alone.[/yellow]")
        return

    if plan.transform:
        apply_static_version(pyproject, plan.transform)
    result = rewrite_version_files(plan.rewrites, root)

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
        rich.print(
            f"[yellow]{escape(question)} No terminal to ask on; skipped.[/yellow]"
        )
        return False
    return bool(questionary.confirm(question, default=default).ask())


def _pin_branch(c: Context, config: Config, root: Path) -> None:
    """
    Write the detected branch rather than `<head>`, so later runs are explicit.
    """
    if not (git := config.active_git) or git.branch != DERIVE_BRANCH:
        return
    repo = GitRepo(runner=ContextRunner(c), root=root)
    if branch := repo.detect_release_branch(git.origin):
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
def authenticate(_: Context, no_verify: bool = False, clear: bool = False) -> None:
    """
    Store a PyPI token in the keyring, replacing any token already there.

    The token is checked against PyPI before it is stored, so a mistyped one is
    caught now rather than at the end of a release, and a rejected one leaves
    the token you already had in place. Pass --no-verify for an index that
    issues tokens PyPI would not recognise, or --clear to drop the stored token
    before being asked for the new one.
    """
    store = TokenStore()
    with _reported():
        if clear:
            rich.print(
                "[yellow]Cleared the stored token.[/yellow]"
                if store.forget()
                else "[yellow]No stored token to clear.[/yellow]"
            )

        existing = store.stored()
        token = _ask_token(replacing=bool(existing))
        store.store(token if no_verify else _verified(token, existing))
    rich.print("[green]Token stored.[/green]")


@task()
def ensure_authenticated(_: Context, show: bool = False) -> None:
    """
    Make sure a PyPI token is available, asking only when there is none.

    Usable as a `pre` task; `release` calls the same resolution in-process, so
    that a `--noop` run is not stopped for a credential it will never use.
    Pass --show to see which token that is and where it came from.
    """
    with _reported():
        source, token = _sourced_token(Config.from_pyproject())

    if show:
        rich.print(f"[green]PyPI token[/green] from the {source}: {mask(token)}")
    else:
        rich.print("[green]PyPI token available.[/green]")


def _verified(token: str, existing: str | None) -> str:
    """
    Check the token, and on a refusal say what that leaves you with.
    """
    try:
        return verify_token(token, notify=_notify)
    except VommitError as error:
        if not existing:
            raise
        raise VommitError(
            f"{error}\nThe token already stored ({mask(existing)}) is untouched."
        ) from error


def _token(config: Config) -> str:
    return _sourced_token(config)[1]


def _sourced_token(config: Config) -> tuple[str, str]:
    """
    A token from the environment, the keyring, or the person at the keyboard.

    `pypi.use_keyring = false` means "ask me every time": the token is used for
    this release and not written anywhere.
    """
    if token := environment_token():
        return ENVIRONMENT, token

    pypi = config.active_pypi
    if pypi and not pypi.use_keyring:
        asked = _ask_token(replacing=False, storing=False)
        return PROMPT, verify_token(asked, notify=_notify)

    store = TokenStore()
    if token := store.stored():
        return KEYRING, token
    if not sys.stdin.isatty():
        # the message names both ways out, so a CI failure is actionable
        return KEYRING, store.require()

    token = verify_token(_ask_token(replacing=False), notify=_notify)
    store.store(token)
    return PROMPT, token


def _ask_token(replacing: bool, storing: bool = True) -> str:
    if not sys.stdin.isatty():
        raise VommitError(
            f"No PyPI token found and no terminal to ask on; set {TOKEN_VAR} "
            "in the environment, or run `vommit authenticate`."
        )
    lead = "Replace the stored PyPI token" if replacing else "PyPI token"
    tail = "" if storing else ", used once and not stored"
    answer = questionary.password(f"{lead} (input hidden{tail}):").ask()
    if not answer:
        raise VommitError("No token entered.")
    return str(answer)


@task()
def release(
    c: Context,
    major: bool = False,
    minor: bool = False,
    patch: bool = False,
    prerelease: bool = False,
    noop: bool = False,
    version: str | None = None,
    allow_dirty: bool = False,
    no_bump: bool = False,
    yes: bool = False,
) -> str | None:
    """
    Bump, build, push and publish.

    Runs the bump, then the configured clean and build commands, then pushes the
    commit and its tag, and publishes last: a failure before the push can still
    be undone, and PyPI never receives a version that the remote does not have.
    Use --noop to see the plan, and --no-bump to publish the current version.
    """
    root = Path.cwd()
    config = Config.from_pyproject(root)

    with _reported():
        result = run_release(
            config=config,
            runner=ContextRunner(c),
            root=root,
            request=ReleaseRequest(
                bump=BumpRequest(
                    level=select_level(major, minor, patch),
                    version=version,
                    prerelease=prerelease,
                    noop=noop,
                    allow_dirty=allow_dirty,
                ),
                no_bump=no_bump,
            ),
            notify=_notify,
            confirm=_asker(config, yes, _bump_question),
            confirm_version=lambda current: _confirm(
                f"No version-worthy changes found; publish {current} as it is?",
                yes=yes,
                default=False,
            ),
            # deliberately not routed through `_asker`: `config.confirm` turns
            # off the routine "shall I release?" question, and must not also
            # silence a warning that something is actually out of step
            confirm_stale=lambda stale: _confirm(
                stale.question, yes=yes, default=False
            ),
            authenticate=lambda: _token(config),
            report_step=_step,
        )

    if result is None:
        rich.print("[yellow]Nothing to release.[/yellow]")
        return None

    _report_release(result)
    if result.cancelled:
        raise Exit(code=1)
    return result.version
