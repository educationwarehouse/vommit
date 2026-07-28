"""
The full release: bump, build, push, publish.

`bump` stops at a tagged commit on the local machine. Everything after that is
here, in the order that keeps the damage of a failure small: the steps that can
still be taken back run first, and the two irreversible ones (pushing a tag,
uploading to PyPI) run last and in that order. PyPI never permits reuploading a
version, so a version reaching PyPI that no remote git repository has is the one
outcome worth designing against.
"""

import dataclasses as dc
import datetime as dt
import shlex
import typing as t
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from .auth import Authenticate, publish_env, require_token
from .bump import BumpRequest, BumpResult, Confirm, Notify, always, run_bump
from .config import CommandConfig, Config, GitConfig
from .errors import VommitError
from .git import GitRepo
from .shell import CommandResult, Runner, shell
from .versioning import UvProject

PYPROJECT = "pyproject.toml"

CLEAN = "clean"
BUILD = "build"
PUBLISH = "publish"
PIPELINE = "release"

# the steps that upload, and so the only ones that get to see the token
CREDENTIALED = frozenset({PUBLISH, PIPELINE})

#: Asks whether to publish the current version when no bump was warranted.
ConfirmVersion = t.Callable[[str], bool]

#: Wraps a step while it runs, so an entrypoint can show a spinner.
StepReporter = t.Callable[[str], AbstractContextManager[t.Any]]


def _unreported(_: str) -> AbstractContextManager[t.Any]:
    return nullcontext()


def _yes(_: str) -> bool:
    return True


@dc.dataclass(frozen=True)
class Step:
    """
    One command of the release pipeline, named after the setting it came from.
    """

    name: str
    command: str


@dc.dataclass(frozen=True)
class ReleaseRequest:
    bump: BumpRequest
    no_bump: bool = False

    def __post_init__(self) -> None:
        if not self.no_bump:
            return
        conflicting = [
            f"--{name}"
            for name, given in (
                ("major", self.bump.level == "major"),
                ("minor", self.bump.level == "minor"),
                ("patch", self.bump.level == "patch"),
                ("prerelease", self.bump.prerelease),
                ("version", bool(self.bump.version)),
            )
            if given
        ]
        if conflicting:
            raise VommitError(
                f"--no-bump publishes the version already in {PYPROJECT}; "
                f"drop {' and '.join(conflicting)}."
            )

    @property
    def noop(self) -> bool:
        return self.bump.noop


@dc.dataclass(frozen=True)
class ReleaseResult:
    version: str
    bump: BumpResult | None
    steps: tuple[Step, ...]
    pushed: bool = False
    published: bool = False
    noop: bool = False
    cancelled: bool = False


def run_release(
    config: Config,
    runner: Runner,
    root: Path,
    request: ReleaseRequest,
    notify: Notify = lambda _: None,
    today: dt.date | None = None,
    confirm: Confirm = always,
    confirm_version: ConfirmVersion = _yes,
    authenticate: Authenticate = require_token,
    report_step: StepReporter = _unreported,
) -> ReleaseResult | None:
    """
    Bump, build, push and publish, stopping at the first step that fails.

    Returns None when there was nothing to release and the offer to publish the
    current version anyway was declined, and a `cancelled` result when the bump
    itself was declined.
    """
    repo = GitRepo(runner=runner, root=root)
    project = UvProject(runner=runner, root=root)

    git = config.active_git
    commands = config.commands
    pypi = config.active_pypi
    steps = _plan(commands, bool(pypi), notify)

    # credentials are resolved before the first write, for the same reason bump
    # checks the tag before creating the commit: discovering afterwards that
    # there is no token would leave a pushed release with nothing on PyPI.
    token = authenticate() if pypi and pypi.use_keyring and not request.noop else None

    bumped = None
    if request.no_bump:
        if git:
            repo.ensure_up_to_date(git, repo.ensure_branch(git, notify), notify)
        version = _current_version(project)
    else:
        bumped = run_bump(
            config=config,
            runner=runner,
            root=root,
            request=request.bump,
            notify=notify,
            today=today,
            confirm=confirm,
        )
        if bumped is None:
            # run_bump has already checked the branch and the remote by now
            version = _current_version(project)
            if not confirm_version(version):
                return None
        elif bumped.cancelled:
            return ReleaseResult(
                version=bumped.version,
                bump=bumped,
                steps=steps,
                noop=True,
                cancelled=True,
            )
        else:
            version = bumped.version

    if request.noop:
        return ReleaseResult(version=version, bump=bumped, steps=steps, noop=True)

    return _execute(
        repo=repo,
        runner=runner,
        root=root,
        git=git,
        commands=commands,
        steps=steps,
        version=version,
        bumped=bumped,
        token=token,
        notify=notify,
        report_step=report_step,
    )


def _current_version(project: UvProject) -> str:
    version = project.current_version()
    if not version:
        raise VommitError(
            f"No version found in {PYPROJECT}; there is nothing to release."
        )
    return version


def _plan(
    commands: CommandConfig | None,
    publishing: bool,
    notify: Notify,
) -> tuple[Step, ...]:
    """
    The commands to run, either as the three configured steps or as one blob.
    """
    if not commands:
        return ()

    if commands.overrides_pipeline:
        if not publishing:
            notify(
                "commands.release is set by hand, so it runs as written; "
                "pypi.enabled = false cannot switch off a step inside it."
            )
        return (Step(PIPELINE, commands.release_command),)

    planned = [Step(CLEAN, commands.clean), Step(BUILD, commands.build)]
    if publishing:
        planned.append(Step(PUBLISH, commands.publish))
    return tuple(planned)


def _execute(
    repo: GitRepo,
    runner: Runner,
    root: Path,
    git: GitConfig | None,
    commands: CommandConfig | None,
    steps: tuple[Step, ...],
    version: str,
    bumped: BumpResult | None,
    token: str | None,
    notify: Notify,
    report_step: StepReporter,
) -> ReleaseResult:
    """
    Run the plan, pushing between building and publishing where that is possible.
    """
    overriding = bool(commands and commands.overrides_pipeline)
    # a hand-written pipeline is one command, so nothing can be slotted into it;
    # pushing first at least keeps PyPI from getting a version git does not have
    before_push = () if overriding else tuple(s for s in steps if s.name != PUBLISH)
    after_push = steps if overriding else tuple(s for s in steps if s.name == PUBLISH)

    for step in before_push:
        _run(runner, root, step, token, report_step, _undoable(version, bumped))

    pushed = _push(repo, git, version, bumped, notify)

    for step in after_push:
        _run(runner, root, step, token, report_step, _released(version, pushed))

    return ReleaseResult(
        version=version,
        bump=bumped,
        steps=steps,
        pushed=pushed,
        published=bool(after_push),
    )


def _push(
    repo: GitRepo,
    git: GitConfig | None,
    version: str,
    bumped: BumpResult | None,
    notify: Notify,
) -> bool:
    """
    Send the release commit and its tag to the remote, as two separate pushes.
    """
    if not git:
        return False

    branch = repo.current_branch()
    repo.push(git.origin, branch)
    notify(f"Pushed {branch} to {git.origin}.")

    # with --no-bump the tag is usually already there; push it only if this
    # machine has one the remote may still be missing
    tag = bumped.tag if bumped else git.format_tag(version)
    if tag and repo.ref_exists(f"refs/tags/{tag}"):
        repo.push_tag(git.origin, tag)
        notify(f"Pushed {tag} to {git.origin}.")
    return True


def _undoable(version: str, bumped: BumpResult | None) -> str:
    if not bumped:
        return "Nothing was pushed or published."
    return (
        f"Nothing has been pushed, so `vommit bump --undo` can still "
        f"take {version} back."
    )


def _released(version: str, pushed: bool) -> str:
    if not pushed:
        return f"{version} was built but not published."
    return (
        f"{version} is committed, tagged and pushed, but not published. "
        f"Retry the upload with `vommit release --no-bump`."
    )


def _from_root(root: Path, command: str) -> str:
    """
    A configured command, run from the project root rather than the caller's
    directory: `uv build` has to find the `pyproject.toml` it is releasing.
    """
    return shell(f"cd {shlex.quote(str(root))} && {command}")


def _run(
    runner: Runner,
    root: Path,
    step: Step,
    token: str | None,
    report_step: StepReporter,
    aftermath: str,
) -> None:
    env = publish_env(token) if step.name in CREDENTIALED else None
    # raised inside the reporter, so that a step which failed is not also
    # reported as having finished
    with report_step(step.name):
        result = runner.run(_from_root(root, step.command), env=env)
        if result.ok:
            return

        raise VommitError(
            "\n".join(
                part
                for part in (
                    f"`{step.name}` failed: {result.error}",
                    _rejected_hint(result),
                    aftermath,
                )
                if part
            )
        )


def _rejected_hint(result: CommandResult) -> str:
    """
    A 403 from an upload is almost always the token, not the package.
    """
    if "403" not in result.stdout + result.stderr:
        return ""
    return "The index refused the credentials; store a new token with `vommit authenticate`."
