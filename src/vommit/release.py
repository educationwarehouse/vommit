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
from .bump import (
    BumpRequest,
    BumpResult,
    Confirm,
    Notify,
    always,
    reject_flags,
    run_bump,
)
from .config import CommandConfig, Config, GitConfig
from .editor import EntryEditor
from .errors import VommitError
from .git import GitRepo
from .shell import CommandResult, Runner, shell
from .versioning import VersionSource, version_source

CLEAN = "clean"
BUILD = "build"
PUBLISH = "publish"
POST_PUBLISH = "post_publish"

#: Asks whether to publish the current version when no bump was warranted.
ConfirmVersion = t.Callable[[str], bool]

#: Asks whether to publish under a tag an earlier attempt left behind.
ConfirmStale = t.Callable[["StaleTag"], bool]

#: Wraps a step while it runs, so an entrypoint can show a spinner.
StepReporter = t.Callable[[str], AbstractContextManager[t.Any]]


def _unreported(_: str) -> AbstractContextManager[t.Any]:
    return nullcontext()


def _yes(_: t.Any) -> bool:
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

    @property
    def noop(self) -> bool:
        return self.bump.noop


@dc.dataclass(frozen=True)
class StaleTag:
    """
    A tag from an earlier attempt, sitting on a commit this release is not
    building. The upload is what really goes out of step: the tag on the
    remote already says what it says, and pushing it again changes nothing.
    """

    tag: str
    version: str
    tagged: str
    tagged_subject: str
    head: str
    head_subject: str

    @property
    def warning(self) -> str:
        return (
            f"Tag '{self.tag}' points at {self.tagged[:8]} "
            f"({self.tagged_subject}), but the release would be built from "
            f"{self.head[:8]} ({self.head_subject}), so {self.version} would "
            "reach the index under a tag that names different code."
        )

    @property
    def question(self) -> str:
        return (
            f"{self.warning}\nPublish {self.version} anyway, leaving "
            f"'{self.tag}' where it is?"
        )


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
    edit: EntryEditor | None = None,
    confirm_version: ConfirmVersion = _yes,
    confirm_stale: ConfirmStale = _yes,
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
    project = version_source(runner, root)

    if request.no_bump:
        # --allow-dirty stays allowed: the build still runs, and it may well
        # have something to say about an unclean tree
        reject_flags(
            request.bump.targeting_flags(include_dirty=False),
            f"--no-bump publishes the version already in {project.manifest_name}",
        )

    git = config.active_git
    commands = config.commands
    pypi = config.active_pypi
    steps = _plan(commands, bool(pypi))

    if git:
        # both of these ask the same question: will the artifact that goes to
        # the index match the commit that goes to the remote? they run before
        # the token is resolved, so a doomed release does not ask for one.
        _require_commits(git)
        _refuse_dirty_tree(repo, request.bump.allow_dirty)

    # credentials are resolved before the first write, for the same reason bump
    # checks the tag before creating the commit: discovering afterwards that
    # there is no token would leave a pushed release with nothing on PyPI.
    publishing = any(step.name == PUBLISH for step in steps)
    token = authenticate() if publishing and not request.noop else None

    bumped = None
    if request.no_bump:
        if git:
            repo.ensure_up_to_date(
                git,
                repo.ensure_branch(git, notify, request.bump.allow_branch),
                notify,
            )
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
            edit=edit,
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

    # only when something is uploaded: with nothing going to an index, the tag
    # on the remote is already whatever it is and this run does not change it.
    # a bump that made its own tag cannot be stale, so this finds nothing there.
    if git and publishing and (stale := _stale_tag(repo, git, version)):
        if request.noop:
            notify(stale.warning)
        elif not confirm_stale(stale):
            return ReleaseResult(
                version=version,
                bump=bumped,
                steps=steps,
                noop=True,
                cancelled=True,
            )

    if request.noop:
        return ReleaseResult(version=version, bump=bumped, steps=steps, noop=True)

    return _execute(
        repo=repo,
        runner=runner,
        root=root,
        git=git,
        steps=steps,
        version=version,
        bumped=bumped,
        token=token,
        notify=notify,
        report_step=report_step,
    )


def _require_commits(git: GitConfig) -> None:
    """
    Refuse a git release that would never record the version it publishes.

    `commit_format = ""` switches the release commit off, which `bump` allows:
    everything it does stays on this machine and `--undo` can take it back.
    Publishing changes that. The bump would be written and staged but never
    committed, so the push sends an unchanged branch, any tag lands on the
    commit *before* the bump, and the index ends up with a version that no
    commit anywhere contains.
    """
    if git.commit_format:
        return
    raise VommitError(
        "Releasing with git enabled needs a release commit, but "
        "`git.commit_format` is empty, so the bump would never be committed "
        "and the version would reach the index without reaching the remote.\n"
        "Set `git.commit_format`, or set `git.enabled = false` to release "
        "without touching git at all."
    )


def _refuse_dirty_tree(repo: GitRepo, allow_dirty: bool) -> None:
    """
    Refuse to build a tree that holds more than the release commit will.

    `bump` only checks the handful of files it writes, which is enough when
    nothing leaves the machine. A release builds the whole working tree and
    pushes only `HEAD`, so anything uncommitted is baked into the artifact and
    missing from the remote; PyPI does not allow a reupload to correct it.
    """
    if allow_dirty:
        return
    if dirty := repo.uncommitted():
        listed = "\n".join(f"  {path}" for path in dirty)
        raise VommitError(
            "The working tree has changes that the release commit would not "
            f"contain, but the build would pick up:\n{listed}\n"
            "Commit or stash them, or pass --allow-dirty to publish the tree "
            "as it stands. Build output such as `dist/` belongs in "
            "`.gitignore`; left untracked it holds up the next release."
        )


def _stale_tag(repo: GitRepo, git: GitConfig, version: str) -> "StaleTag | None":
    """
    A tag for this version that an earlier attempt left on another commit.

    Only possible when this run did not make the tag itself: `--no-bump`, or a
    bump that turned out to have nothing to do. Worth looking for exactly then,
    because fixing something and retrying without re-bumping is what `--no-bump`
    is for, and the fix moves `HEAD` off the tag.
    """
    tag = git.format_tag(version)
    if not tag:
        return None
    tagged = repo.tag_commit(tag)
    head = repo.head_commit()
    if tagged is None or tagged == head:
        return None
    return StaleTag(
        tag=tag,
        version=version,
        tagged=tagged,
        tagged_subject=repo.subject(tagged),
        head=head,
        head_subject=repo.subject(head),
    )


def _current_version(project: VersionSource) -> str:
    version = project.current_version()
    if not version:
        raise VommitError(
            f"No version found in {project.manifest_name}; there is nothing to release."
        )
    return version


def _plan(commands: CommandConfig | None, publishing: bool) -> tuple[Step, ...]:
    """
    The commands to run, in order, skipping the ones left empty.
    """
    if not commands:
        return ()

    configured = [(CLEAN, commands.clean), (BUILD, commands.build)]
    if publishing:
        # both belong to the publishing half of the pipeline, and both are
        # kept even when `publish` itself is empty: emptying it while setting
        # `post_publish` is how someone says "build and tag here, upload my
        # own way", and dropping the one step they configured would surprise
        configured += [
            (PUBLISH, commands.publish),
            (POST_PUBLISH, commands.post_publish),
        ]
    return tuple(Step(name, command) for name, command in configured if command.strip())


def _execute(
    repo: GitRepo,
    runner: Runner,
    root: Path,
    git: GitConfig | None,
    steps: tuple[Step, ...],
    version: str,
    bumped: BumpResult | None,
    token: str | None,
    notify: Notify,
    report_step: StepReporter,
) -> ReleaseResult:
    """
    Run the plan, with the push slotted in between building and publishing.
    """
    after_push = tuple(step for step in steps if step.name in (PUBLISH, POST_PUBLISH))
    before_push = tuple(step for step in steps if step not in after_push)

    for step in before_push:
        _run(runner, root, step, token, report_step, _undoable(version, bumped))

    pushed = _push(repo, git, version, bumped, notify)

    for step in after_push:
        _run(runner, root, step, token, report_step, _aftermath(step, version, pushed))

    return ReleaseResult(
        version=version,
        bump=bumped,
        steps=steps,
        pushed=pushed,
        published=any(step.name == PUBLISH for step in steps),
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


def _aftermath(step: Step, version: str, pushed: bool) -> str:
    """
    What the state is once this step has failed, said plainly.
    """
    if step.name == POST_PUBLISH:
        return f"{version} is published; only the follow-up command failed."
    return _released(version, pushed)


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
    env = publish_env(token) if step.name == PUBLISH else None
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
