import typing as t
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .changelog import Changelog, ChangelogUpdate
from .commits import VersionBump, highest_version_bump
from .config import Config
from .errors import VommitError
from .git import GitRepo
from .shell import Runner
from .versioning import UvProject

Notify = t.Callable[[str], None]

PYPROJECT = "pyproject.toml"
LOCKFILE = "uv.lock"


@dataclass(frozen=True)
class BumpRequest:
    level: VersionBump | None = None
    version: str | None = None
    prerelease: bool = False
    noop: bool = False
    allow_dirty: bool = False

    def __post_init__(self) -> None:
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


@dataclass(frozen=True)
class BumpResult:
    previous: str | None
    version: str
    level: VersionBump | None
    entry: str | None
    changelog_path: Path | None
    commit_message: str | None
    tag: str | None
    noop: bool


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


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def run_bump(
    config: Config,
    runner: Runner,
    root: Path,
    request: BumpRequest,
    notify: Notify = lambda _: None,
    today: date | None = None,
) -> BumpResult | None:
    """
    Bump the version, update the changelog, commit and tag.

    Returns None when the commits since the last release do not warrant a bump.
    Nothing is written until every step that can fail has been checked, so a
    missing placeholder or a wrong branch leaves the project untouched.
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
    tag_glob = config.git.tag_glob if config.git else None
    commit_messages = repo.commit_messages_since(repo.last_tag(tag_glob))

    level = request.level or highest_version_bump(
        config.resolve_version_bump_from_commit(message) for message in commit_messages
    )
    if not request.version and level is None:
        return None

    previous = project.current_version()
    next_version = (
        project.preview_set(request.version)
        if request.version
        else project.preview_bump(t.cast(VersionBump, level), request.prerelease)
    )

    changelog = (
        Changelog(settings=changelog_settings, root=root)
        if changelog_settings
        else None
    )
    update: ChangelogUpdate | None = None
    if changelog:
        update = changelog.plan(
            next_version, config.commit_entries(commit_messages), today
        )

    touched = [PYPROJECT, *([_relative(update.path, root)] if update else [])]
    tag = git.format_tag(next_version) if git else None
    commit_message = git.format_commit(next_version) if git else None

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
    )
    if request.noop:
        return result

    applied = (
        project.apply_set(request.version)
        if request.version
        else project.apply_bump(t.cast(VersionBump, level), request.prerelease)
    )
    if applied != next_version:
        raise VommitError(
            f"`uv version` produced {applied}, but {next_version} was planned; "
            "the project changed underneath us."
        )

    if update:
        update.apply()

    if git:
        if project.lockfile.exists():
            touched.append(LOCKFILE)
        repo.add(touched)
        if commit_message:
            _commit(repo, commit_message, next_version)
        if tag:
            repo.tag(tag)

    return result


def _commit(repo: GitRepo, message: str, version: str) -> None:
    try:
        repo.commit(message)
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
