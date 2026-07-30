import dataclasses as dc
import datetime as dt
import typing as t
from pathlib import Path

from .changelog import Changelog, ChangelogUpdate
from .commits import VersionBump, highest_version_bump
from .config import Config
from .errors import VommitError
from .git import GitRepo, resolve_author
from .helpers import relative_path
from .shell import Runner
from .versioning import UvProject, is_prerelease, plan_bump

Notify = t.Callable[[str], None]
Confirm = t.Callable[["BumpResult"], bool]

PYPROJECT = "pyproject.toml"
LOCKFILE = "uv.lock"


def always(_: t.Any) -> bool:
    """
    The default answer for callers that have nobody to ask.
    """
    return True


def reject_flags(flags: list[str], because: str) -> None:
    """
    Refuse flags that contradict the mode the user asked for.
    """
    if flags:
        raise VommitError(f"{because}; drop {' and '.join(flags)}.")


@dc.dataclass(frozen=True)
class BumpRequest:
    level: VersionBump | None = None
    version: str | None = None
    prerelease: bool = False
    noop: bool = False
    allow_dirty: bool = False
    undo: bool = False

    def __post_init__(self) -> None:
        if self.undo:
            self._reject_alongside_undo()
            return
        if not self.version:
            return
        if self.level:
            raise VommitError(
                f"--version {self.version} sets an exact version; drop --{self.level}."
            )
        if self.prerelease:
            raise VommitError(
                f"--version {self.version} sets an exact version; "
                "--prerelease would be ignored."
            )

    def targeting_flags(self, include_dirty: bool = True) -> list[str]:
        """
        The flags that aim this request at a new version.

        Anything that takes the version as given instead of choosing one
        (`--undo`, `--no-bump`) has the same set to reject, so the set is
        written once here rather than once per caller.
        """
        return [
            f"--{name}"
            for name, given in (
                ("major", self.level == "major"),
                ("minor", self.level == "minor"),
                ("patch", self.level == "patch"),
                ("prerelease", self.prerelease),
                ("version", bool(self.version)),
                ("allow-dirty", include_dirty and self.allow_dirty),
            )
            if given
        ]

    def _reject_alongside_undo(self) -> None:
        """
        --undo takes back the last release; it cannot also aim at a new one.
        """
        reject_flags(self.targeting_flags(), "--undo goes back to the previous release")


@dc.dataclass(frozen=True)
class BumpResult:
    previous: str | None
    version: str
    level: VersionBump | None
    entry: str | None
    changelog_path: Path | None
    commit_message: str | None
    tag: str | None
    noop: bool
    prerelease: bool = False
    # declined at the confirmation step; nothing was written, same as a noop
    cancelled: bool = False


def select_level(
    major: bool = False,
    minor: bool = False,
    patch: bool = False,
) -> VersionBump | None:
    """
    Turn the mutually exclusive CLI flags into one level (None = derive it).
    """
    chosen = [
        level
        for level, picked in (("major", major), ("minor", minor), ("patch", patch))
        if picked
    ]
    if len(chosen) > 1:
        raise VommitError(
            f"Pick one bump level, not {' and '.join(f'--{level}' for level in chosen)}."
        )
    return t.cast(VersionBump | None, chosen[0] if chosen else None)


def run_bump(
    config: Config,
    runner: Runner,
    root: Path,
    request: BumpRequest,
    notify: Notify = lambda _: None,
    today: dt.date | None = None,
    confirm: Confirm = always,
) -> BumpResult | None:
    """
    Bump the version, update the changelog, commit and tag.

    Returns None when the commits since the last release do not warrant a bump,
    and a `cancelled` result when `confirm` says no. Nothing is written until
    every step that can fail has been checked, so a missing placeholder or a
    wrong branch leaves the project untouched.
    """
    git = config.active_git
    changelog_settings = config.active_changelog

    repo = GitRepo(runner=runner, root=root)
    project = UvProject(runner=runner, root=root)

    if git:
        branch = repo.ensure_branch(git, notify)
        repo.ensure_up_to_date(git, branch, notify)

    # reading history is how the bump level is found at all, so it happens even
    # when git integration (branch checks, committing, tagging) is switched off.
    # only a changelog that lists prereleases has a reason to stop at one: it
    # has already reported those commits, so repeating them would duplicate.
    aggregating = not (changelog_settings and changelog_settings.include_prereleases)
    tag_glob = config.git.tag_glob if config.git else None
    stable_tag = repo.last_stable_tag(tag_glob, config.tag_version)
    baseline = config.tag_version(stable_tag)

    # aggregating means the window that feeds the changelog also decides the
    # level, so a prerelease series that has gone quiet can still be released.
    since = stable_tag if aggregating else repo.last_tag(tag_glob)
    commit_messages = repo.commit_messages_since(since)

    level = request.level or highest_version_bump(
        config.resolve_version_bump_from_commit(message) for message in commit_messages
    )
    if not request.version and level is None:
        return None

    previous = project.current_version()
    plan = (
        [request.version]
        if request.version
        else plan_bump(
            current=previous,
            level=t.cast(VersionBump, level),
            baseline=baseline,
            prerelease_token=config.prerelease_token if request.prerelease else None,
        )
    )
    next_version = project.preview(plan)
    prerelease = is_prerelease(next_version)

    changelog = (
        Changelog(settings=changelog_settings, root=root)
        if changelog_settings
        else None
    )
    update: ChangelogUpdate | None = None
    if changelog_settings and prerelease and not changelog_settings.include_prereleases:
        notify(
            f"{next_version} is a prerelease; its changes stay unlisted until "
            "the next release."
        )
    elif changelog:
        update = changelog.plan(
            next_version, config.commit_entries(commit_messages), today
        )

    touched = [PYPROJECT]
    if update:
        touched.append(relative_path(update.path, root))

    tag = git.format_tag(next_version) if git else None
    commit_message = git.format_commit(next_version) if git else None
    # guarded by the commit, like the tag check below: `commit_format = ""` is a
    # supported bump that stages without committing, and refusing it over an
    # author that is never passed to git would block it for no gain.
    author = resolve_author(git.commit_author) if git and commit_message else None

    if git:
        if not request.allow_dirty:
            _refuse_dirty(repo, touched)
        if tag:
            # checked here rather than at tagging time: finding out afterwards
            # would leave a release commit that never gets a tag.
            repo.ensure_tag_available(tag)

    result = BumpResult(
        previous=previous,
        version=next_version,
        level=level,
        entry=update.entry if update else None,
        changelog_path=update.path if update else None,
        commit_message=commit_message,
        tag=tag,
        noop=request.noop,
        prerelease=prerelease,
    )
    if request.noop:
        return result
    if not confirm(result):
        return dc.replace(result, noop=True, cancelled=True)

    lockfile_ignored = bool(git and repo.ignores(LOCKFILE))
    applied = project.apply(plan, frozen=lockfile_ignored)
    if applied != next_version:
        raise VommitError(
            f"`uv version` produced {applied}, but {next_version} was planned; "
            "the project changed underneath us."
        )

    if update:
        update.apply()

    if git:
        if project.lockfile.exists() and not lockfile_ignored:
            touched.append(LOCKFILE)
        try:
            repo.add(touched)
        except VommitError as error:
            raise VommitError(
                f"{error}\nNothing has been pushed, so `vommit bump --undo` can still "
                f"take {next_version} back."
            ) from error
        if commit_message:
            _commit(repo, commit_message, next_version, author=author)
        if tag:
            repo.tag(tag)

    return result


def _commit(
    repo: GitRepo, message: str, version: str, author: str | None = None
) -> None:
    try:
        repo.commit(message, author=author)
    except VommitError as error:
        # the version is already written by now; say where that leaves things
        raise VommitError(
            f"{error}\nThe changes for {version} are staged but not committed, "
            "and no tag was created."
        ) from error


def _refuse_dirty(repo: GitRepo, paths: list[str]) -> None:
    dirty = repo.dirty_paths(paths)
    if dirty:
        raise VommitError(
            f"Uncommitted changes in {', '.join(dirty)}; "
            "commit or stash them first, or pass --allow-dirty."
        )
