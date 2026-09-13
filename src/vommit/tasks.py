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
from . import npm
from .build import (
    FIX_ALL,
    FIXABLE_WARNING_IDS,
    ProjectWarning,
    project_warnings,
    requested_fixes,
)
from .bump import (
    BumpAnswer,
    BumpRequest,
    BumpResult,
    always,
    run_bump,
    select_level,
)
from .changelog import Changelog
from .config import DERIVE_BRANCH, TOML_KEY, Config
from .editor import edit_entry
from .errors import VommitError
from .git import GitRepo
from .helpers import relative_path
from .interactive import Prompts
from .migrate import (
    PSR_KEY,
    Migration,
    MigrationReport,
    UnsupportedPolicy,
    VersionPlan,
    apply_static_version,
    discover_version_files,
    parse_unsupported_policy,
    plan_version_migration,
    read_psr,
    rewrite_version_files,
    strip_psr,
    translate,
)
from .release import ReleaseRequest, ReleaseResult, Step, run_release
from .scaffold import (
    DEFAULT_BRANCH,
    DEFAULT_VENV,
    NO_VENV,
    Defaults,
    ScaffoldRequest,
    ScaffoldResult,
    ensure_uv_lock_ignored,
    initial_commit_message,
    plan_request,
    run_scaffold,
)
from .shell import ContextRunner
from .undo import UndoPlan, UndoResult, run_undo
from .versioning import CargoProject, NpmProject, VersionSource, version_source

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
    except KeyboardInterrupt as interrupt:
        # every prompt uses `unsafe_ask`, so ctrl-c arrives here rather than
        # answering None and letting the command carry on with defaults
        rich.print("[yellow]Interrupted.[/yellow]")
        raise Exit(code=1) from interrupt


def _notify(message: str) -> None:
    # escaped: these messages name things like [project].license, and rich would
    # read the brackets as a style tag and swallow the word
    rich.print(f"[blue]{escape(message)}[/blue]")


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
        return bool(questionary.confirm(text, default=True).unsafe_ask())

    return ask


# The answers the release question takes, and what each one means.
BUMP_CHOICES: dict[str, BumpAnswer] = {
    "yes": "yes",
    "edit the changelog entry": "edit",
    "no": "no",
}


def _bump_asker(
    config: Config, yes: bool
) -> t.Callable[[BumpResult], bool | BumpAnswer]:
    """
    The release question, with the changelog entry an answer of its own.

    Editing is only offered when there is an entry to edit; without one the
    question is the same two-way confirmation every other step asks.
    """
    if yes or not config.confirm:
        return always

    def ask(result: BumpResult) -> bool | BumpAnswer:
        text = _bump_question(result)
        if not sys.stdin.isatty():
            rich.print(f"[yellow]{text} No terminal to ask on; pass --yes.[/yellow]")
            return False
        if not result.entry:
            return bool(questionary.confirm(text, default=True).unsafe_ask())

        choice = questionary.select(
            text, choices=list(BUMP_CHOICES), default="yes"
        ).unsafe_ask()
        return BUMP_CHOICES[str(choice)]

    return ask


def _editor(c: Context) -> t.Callable[[str], str]:
    return lambda entry: edit_entry(entry, ContextRunner(c), _notify)


@task(
    # long flags only: a dozen parameters here left invoke deriving short flags
    # like `-y` for `--python`, and one collision produced a bare `--`
    auto_shortflags=False,
    help={
        "project-name": "Directory and package name to create.",
        "non-interactive": "Take every answer from the flags and defaults.",
        "python": "Minimum Python version (default: the interpreter running vommit).",
        "description": "Project description; omitted from pyproject.toml when empty.",
        "license": f"SPDX identifier for [project].license, or '{licenses.NO_LICENSE}'.",
        "branch": "Release branch (default: the one git creates).",
        "remote": "URL to add as 'origin'.",
        "message": "Initial commit message; pass an empty one to make no commit.",
        "push": "Push the initial commit and set the upstream.",
        "venv": f"Directory for the project's own environment, or '{NO_VENV}'.",
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
    venv: str = DEFAULT_VENV,
    pin_python: bool = False,
    no_workspace: bool = False,
) -> None:
    """
    Create a new uv package, configure vommit in it, and make its first commit.

    Asks for the Python floor, description, license, release branch, remote,
    commit message and where the project's environment goes; every flag here
    pre-fills its question, and `--non-interactive` takes the answers as given.
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
            commit_message=initial_commit_message(message),
            push=push,
            venv=None if venv.strip().lower() == NO_VENV else venv.strip(),
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
            configure=lambda root, settled: _configure_new(
                c, root, settled, non_interactive
            ),
            notify=_notify,
        )
        _report_scaffold(result)


def _detected_branch(runner: ContextRunner, cwd: Path) -> str:
    """
    The branch name a `git init` here would produce, for the prompt's default.
    """
    return GitRepo(runner=runner, root=cwd).main_branch_local() or DEFAULT_BRANCH


def _configure_new(
    c: Context,
    root: Path,
    branch: str,
    non_interactive: bool,
) -> None:
    """
    Write the vommit config into the project `init` just created.

    The branch is handed over rather than left to be derived: `init` has already
    renamed the checkout to it, and a remote whose HEAD names another branch
    would otherwise win and configure a release from a branch nobody is on.
    """
    setup(c, non_interactive=non_interactive, project_dir=str(root), branch=branch)


def _report_scaffold(result: ScaffoldResult) -> None:
    rich.print(f"[green]Created[/green] {escape(str(result.root))}")
    rich.print(f"  [dim]branch[/dim]  {escape(result.branch)}")
    if result.license_id:
        rich.print(
            f"  [dim]license[/dim] {escape(result.license_id)} [dim](add a LICENSE file)[/dim]"
        )
    if result.remote:
        rich.print(f"  [dim]remote[/dim]  {escape(result.remote)}")
    if result.venv:
        installed = "editable" if result.installed else "not installed"
        rich.print(
            f"  [dim]venv[/dim]    {escape(result.venv)} [dim]({installed})[/dim]"
        )
    if not result.committed:
        rich.print("[yellow]No initial commit was made.[/yellow]")
    if result.remote and not result.pushed:
        rich.print("[blue]Nothing was pushed yet.[/blue]")


@task(
    hookable=False,
    help={
        "non-interactive": "Take every answer from the flags and defaults.",
        "project-dir": "Project to configure (default: the current directory).",
        "mode": "'missing' asks only about unset settings; 'all' revisits every one.",
        "branch": "Release branch, when it should not come from the remote.",
        "fix": (
            "Apply the named build warnings' fixes without asking, comma "
            f"separated, or '{FIX_ALL}' for every one that has a fix. "
            f"Fixable: {', '.join(sorted(FIXABLE_WARNING_IDS))}."
        ),
    },
)
def setup(
    c: Context,
    non_interactive: bool = False,
    project_dir: str | None = None,
    mode: SetupMode = "missing",
    branch: str | None = None,
    fix: str | None = None,
) -> None:
    """
    Create or complete the vommit config in this project's pyproject.toml.

    Hands over to `migrate` when it finds a python-semantic-release v7 config
    and nothing of vommit's own. `--mode=all` revisits every setting rather than
    only the missing ones. `--branch` supplies the release branch that would
    otherwise be derived from the remote or the checkout. `--fix` applies build
    warnings' fixes without asking, which is how a non-interactive run makes
    them at all.
    """
    root = _project_root(project_dir)
    pyproject = root / "pyproject.toml"

    with _reported():
        # inside the report: a misspelled id is a VommitError, and the flag is
        # worth rejecting before anything is written rather than after
        autofix = requested_fixes(fix)
        if mode == "all" and non_interactive:
            # `--mode=all` asks about every setting and `--non-interactive`
            # answers none, so together they can only overwrite what is
            # already configured
            raise VommitError(
                "--mode=all revisits every setting, which needs someone to "
                "answer for them; it cannot be combined with "
                "--non-interactive. Drop one of the two, or pass --fix to "
                "make the build-warning changes unattended."
            )

        if _offers_migration(pyproject, non_interactive):
            return migrate(c, project_dir=project_dir)

        runner = ContextRunner(c)
        # resolved once and passed down: each call can shell out to `uv version`
        project = version_source(runner, root)

        config = Config.from_pyproject(root)
        configured = Config.has_pyproject_config(pyproject)
        present_paths = (
            Config.configured_key_paths(pyproject)
            if mode == "missing" and configured
            else None
        )
        # applies regardless of interactivity: a non-interactive setup gets no
        # prompt to override a bad default, so it needs the right one even more
        if isinstance(project, NpmProject):
            _suggest_npm_defaults(config, present_paths, root)

        if not non_interactive:
            if mode == "missing" and Config.is_complete(pyproject):
                rich.print(
                    "[blue]Nothing to configure: existing vommit config is already complete.[/blue]"
                )
            else:
                config = Config.interactive(config, present_paths=present_paths)

        _write_config(c, config, root, pyproject, branch)
        if ensure_uv_lock_ignored(root):
            rich.print("[blue]Added uv.lock to .gitignore.[/blue]")
        # a crate-backed project keeps `dynamic = ["version"]` on purpose, so the
        # offer below, which exists to make [project].version bumpable, has
        # nothing to fix and would report the deliberate shape as a problem
        if _report_version_source(project):
            _offer_version_files(root, pyproject, non_interactive)
        _report_build(root, pyproject, config, non_interactive, autofix)
        if isinstance(project, NpmProject):
            _offer_npm_token(runner, non_interactive)


def _offer_npm_token(runner: ContextRunner, non_interactive: bool) -> None:
    """
    Nudges toward a publish credential, saying nothing once one is configured.

    Non-interactive only prints what to do; interactive offers to write it to
    `~/.npmrc` there and then, so setup and a first release happen in one
    sitting instead of the release failing on this days later.
    """
    if npm.has_auth_token():
        return

    rich.print(f"[yellow]{npm.token_instructions()}[/yellow]")
    if non_interactive:
        return

    if not _confirm(
        "Paste the token now to write it to ~/.npmrc?", yes=False, default=False
    ):
        return

    token = questionary.password("npm/bun publish token:").unsafe_ask()
    token = (token or "").strip()
    if not token:
        rich.print("[yellow]No token entered; nothing written.[/yellow]")
        return

    # checked first, as `authenticate` does: a typo here would otherwise
    # surface at the first release, days later
    npm.verify_token(token, runner)
    npm.write_auth_token(token)
    rich.print(f"[green]Wrote a publish token to {npm.NPMRC}.[/green]")


def _suggest_npm_defaults(
    config: Config,
    present_paths: set[str] | None,
    root: Path,
) -> None:
    """
    Swap npm-flavored defaults into whichever of `commands.build`,
    `commands.publish` and `pypi.enabled` the prompt is about to ask for. A
    field already configured (named in `present_paths`) is left alone.

    `pypi.enabled` used to be what let the publish step run at all, so it had
    to stay on for projects that had never touched PyPI. Publishing no longer
    depends on it (see `run_release`), so it can default off here.
    """
    if config.commands is not None:
        suggested = npm.suggested_commands(root)
        if suggested is not None:
            if present_paths is None or "commands.build" not in present_paths:
                config.commands.build = suggested.build
            if present_paths is None or "commands.publish" not in present_paths:
                config.commands.publish = suggested.publish

    if config.pypi is not None and (
        present_paths is None or "pypi.enabled" not in present_paths
    ):
        config.pypi.enabled = False


def _report_version_source(source: VersionSource) -> bool:
    """
    Whether the version is `[project].version`, saying so when it is not.

    Worth stating out loud: everything else vommit prints talks about
    pyproject.toml, and a bump that edits a different file is a surprise the
    first time it happens.
    """
    if not isinstance(source, CargoProject):
        return True
    rich.print(
        f"[blue]The version comes from {source.manifest_name} "
        f"({escape('`[package].version`')}), so that is what `vommit bump` "
        f'moves; pyproject.toml keeps `dynamic = ["version"]`.[/blue]'
    )
    return False


def _report_build(
    root: Path,
    pyproject: Path,
    config: Config,
    non_interactive: bool,
    autofix: set[str],
) -> None:
    """
    Say what would go wrong at build time, while nothing has been released yet,
    and offer the two ways out: fix it, or stop being told about it.

    The ignore key is offered second and only when the fix was declined (or
    there is none to offer): silencing a warning someone would have fixed in
    one keystroke is the worse of the two outcomes.

    An id in `autofix` skips both questions and writes the change. The warning
    is still printed: a run that edited the project should say what it edited,
    and an id asked for that turns out to have no fix has to say that too.
    """
    for warning in project_warnings(root, config):
        rich.print(f"[yellow]{escape(warning.message)}[/yellow]")
        if warning.id in autofix:
            _apply_fix(warning)
            continue
        if non_interactive:
            continue
        if _offer_fix(warning):
            continue
        _offer_ignore(warning, config, pyproject)


def _apply_fix(warning: ProjectWarning) -> None:
    """
    Take a `--fix` id at its word, or say why the id had nothing to write.

    A fixable id still arrives without a `fix` when the project's shape blocks
    the change: Hatchling with a `[tool.hatch.build]` table, or a `hatch build`
    that does more than build. The warning above already named the blocker, so
    this only has to say that nothing was written.
    """
    if not (fix := warning.fix):
        rich.print(
            f'[yellow]Nothing applied for "{escape(warning.id)}": '
            "this project's fix is not mechanical, see above.[/yellow]"
        )
        return
    fix.apply()
    rich.print(f"[green]{escape(fix.done)}[/green]")


def _offer_fix(warning: ProjectWarning) -> bool:
    if not (fix := warning.fix):
        return False
    if not _confirm(fix.question, yes=False):
        return False
    fix.apply()
    rich.print(f"[green]{escape(fix.done)}[/green]")
    return True


def _offer_ignore(warning: ProjectWarning, config: Config, pyproject: Path) -> None:
    question = f'Stop reporting "{warning.id}" for this project?'
    if not _confirm(question, yes=False, default=False):
        return
    config.ignoring(warning.id).write_to_pyproject(pyproject)
    rich.print(f'[blue]Added "{escape(warning.id)}" to [tool.vommit] ignore.[/blue]')


def _offer_version_files(root: Path, pyproject: Path, non_interactive: bool) -> None:
    """
    Offer to stop this project writing its version down in two places.

    A source file holding a literal is the version vommit does not bump: the
    next release moves `[project].version` and leaves `__version__` behind.
    Reading it from the installed metadata instead has one version again.
    """
    try:
        plan = plan_version_migration(
            pyproject, discover_version_files(root, pyproject)
        )
    except VommitError as error:
        # the config is already written and works; the version shape is a
        # separate problem, and naming it beats failing the whole command
        rich.print(
            f"[yellow]The version could not be made bumpable: "
            f"{escape(str(error))}[/yellow]"
        )
        return

    if plan.empty:
        return

    if non_interactive:
        rich.print("[blue]Run `vommit setup` to fix how the version is stored:[/blue]")
        for step in plan.steps:
            rich.print(f"  [dim]-[/dim] {escape(step)}")
        return

    _apply_version_plan(plan, root, pyproject, yes=False)


def _write_config(
    c: Context,
    config: Config,
    root: Path,
    pyproject: Path,
    branch: str | None = None,
    replaces: str | None = None,
) -> None:
    """
    The tail every setup path shares: pin the branch, write, make a changelog.
    """
    _pin_branch(c, config, root, branch)
    config.write_to_pyproject(pyproject, replaces=replaces)
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

        # written into the v7 table's place, so the file keeps its shape and the
        # config a reader is looking for is where the old one was
        _write_config(c, config, root, pyproject, replaces=PSR_KEY)
        rich.print(f"[green]Wrote[/green] {escape(f'[{TOML_KEY}]')} to {pyproject}")

        _apply_version_plan(plan, root, pyproject, yes)
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
    ).unsafe_ask()
    return next(
        (choice for choice, label in MIGRATE_CHOICES.items() if label == answer),
        "stop",
    )


def _apply_version_plan(
    plan: VersionPlan,
    root: Path,
    pyproject: Path,
    yes: bool,
) -> None:
    """
    Carry out a version plan, once it has been agreed to.

    Shared by `migrate`, which learns the files from v7's `version_variable`,
    and `setup`, which finds them by convention.
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
    result = rewrite_version_files(plan.rewrites, root, plan.distribution)

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
    return bool(questionary.confirm(question, default=default).unsafe_ask())


def _pin_branch(
    c: Context,
    config: Config,
    root: Path,
    branch: str | None = None,
) -> None:
    """
    Write a concrete branch rather than `<head>`, so later runs are explicit.

    A branch given by the caller wins over detection: it is the one this project
    was just put on, and the remote's HEAD may well name a different one.
    """
    if not (git := config.active_git) or git.branch != DERIVE_BRANCH:
        return
    if branch:
        git.branch = branch
        return
    repo = GitRepo(runner=ContextRunner(c), root=root)
    if detected := repo.detect_release_branch(git.origin):
        git.branch = detected


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
    allow_branch: bool = False,
    no_changelog: bool = False,
    edit: bool = False,
    undo: bool = False,
    yes: bool = False,
) -> str | None:
    """
    Bump the version, write the changelog, then commit and tag.

    Nothing is written until every check has passed, so a failed run leaves the
    project as it was. Use --noop to see the new version and changelog entry,
    --edit to write the entry yourself, --yes to skip the confirmation,
    --allow-branch to bump from a branch other than the release branch, and
    --undo to take the last release back.
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
                allow_branch=allow_branch,
                no_changelog=no_changelog,
                edit=edit,
            ),
            notify=_notify,
            confirm=_bump_asker(config, yes),
            edit=_editor(c),
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
    Store a publish credential, replacing any already there.

    For an npm-versioned project (see NpmProject) that means an `_authToken`
    line in ~/.npmrc, checked with a read-only `whoami` before it is written;
    for everything else, a PyPI token in the keyring, exactly as before. Pass
    --no-verify to skip that check, or --clear to drop the stored credential
    before being asked for a new one.
    """
    runner = ContextRunner(_)
    root = Path.cwd()
    with _reported():
        if isinstance(version_source(runner, root), NpmProject):
            _authenticate_npm(runner, no_verify=no_verify, clear=clear)
        else:
            _authenticate_pypi(no_verify=no_verify, clear=clear)


def _authenticate_pypi(no_verify: bool, clear: bool) -> None:
    store = TokenStore()
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


def _authenticate_npm(runner: ContextRunner, no_verify: bool, clear: bool) -> None:
    if clear:
        rich.print(
            "[yellow]Cleared the stored token.[/yellow]"
            if npm.clear_auth_token()
            else "[yellow]No stored token to clear.[/yellow]"
        )

    token = _ask_npm_token(replacing=npm.has_auth_token())
    if not no_verify:
        npm.verify_token(token, runner)
    npm.write_auth_token(token)
    rich.print(f"[green]Token stored in {npm.NPMRC}.[/green]")


def _ask_npm_token(replacing: bool) -> str:
    if not sys.stdin.isatty():
        raise VommitError(
            f"No npm/bun token found and no terminal to ask on; put an "
            f"`_authToken` line in {npm.NPMRC} yourself, or run `vommit "
            "authenticate` where there is one."
        )
    rich.print(f"[blue]{npm.TOKEN_ADVICE}[/blue]")
    lead = "Replace the stored npm/bun token" if replacing else "npm/bun token"
    answer = questionary.password(f"{lead} (input hidden):").unsafe_ask()
    if not answer:
        raise VommitError("No token entered.")
    return str(answer)


@task()
def ensure_authenticated(_: Context, show: bool = False) -> None:
    """
    Make sure a publish credential is available, asking only when there is none.

    For an npm-versioned project that means an `_authToken` line in
    ~/.npmrc; for everything else, a PyPI token, exactly as before. Usable
    as a `pre` task; `release` calls the same resolution in-process, so
    that a `--noop` run is not stopped for a credential it will never use.
    Pass --show to see which one that is and where it came from.

    The npm side only looks at ~/.npmrc. A project-local `.npmrc` or a token
    in the environment publishes perfectly well without one, so a project
    authenticating either of those ways should not make this a `pre` task.
    """
    runner = ContextRunner(_)
    root = Path.cwd()
    with _reported():
        if isinstance(version_source(runner, root), NpmProject):
            if not npm.has_auth_token():
                raise VommitError(
                    f"No npm/bun token found in {npm.NPMRC}; run `vommit "
                    "authenticate` to store one."
                )
            if show:
                rich.print(f"[green]npm/bun token[/green] configured in {npm.NPMRC}.")
            else:
                rich.print("[green]npm/bun token available.[/green]")
            return
        source, token = _sourced_token(Config.from_pyproject(root))

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
    answer = questionary.password(f"{lead} (input hidden{tail}):").unsafe_ask()
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
    allow_branch: bool = False,
    no_bump: bool = False,
    no_changelog: bool = False,
    edit: bool = False,
    yes: bool = False,
) -> str | None:
    """
    Bump, build, push and publish.

    Runs the bump, then the configured clean and build commands, then pushes the
    commit and its tag, and publishes last: a failure before the push can still
    be undone, and PyPI never receives a version that the remote does not have.
    Use --noop to see the plan, --edit to write the changelog entry yourself,
    --allow-branch to release from a branch other than the release branch (a
    prerelease cut from a feature branch), and --no-bump to publish the current
    version.
    """
    root = Path.cwd()
    config = Config.from_pyproject(root)

    with _reported():
        # said before the bump, so a warning about the artifact arrives while
        # nothing has been written yet. Report-only here: a release is the wrong
        # moment to be asked a configuration question, and `setup` is where the
        # same warnings come with the offer to act on them.
        for warning in project_warnings(root, config):
            rich.print(f"[yellow]{escape(warning.message)}[/yellow]")

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
                    allow_branch=allow_branch,
                    no_changelog=no_changelog,
                    edit=edit,
                ),
                no_bump=no_bump,
            ),
            notify=_notify,
            confirm=_bump_asker(config, yes),
            edit=_editor(c),
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
