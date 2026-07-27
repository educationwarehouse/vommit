import typing as t
from dataclasses import dataclass, replace
from pathlib import Path

from .bump import LOCKFILE, PYPROJECT, Notify, always
from .changelog import Changelog
from .config import Config
from .errors import VommitError
from .git import GitRepo
from .shell import Runner
from .versioning import UvProject


@dataclass(frozen=True)
class UndoPlan:
    """
    What taking back the last release would involve, before any of it happens.

    `commit` is set when there is a release commit to drop, in which case
    resetting to `previous_commit` restores everything at once. Without one the
    changes are still sitting in the working tree and get reversed file by file.
    """

    version: str
    previous: str
    tag: str | None = None
    commit: str | None = None
    previous_commit: str | None = None
    changelog_path: Path | None = None

    @property
    def rewinds_history(self) -> bool:
        return self.commit is not None

    @property
    def paths(self) -> tuple[str, ...]:
        if self.rewinds_history:
            return ()
        changelog = (str(self.changelog_path),) if self.changelog_path else ()
        return (PYPROJECT, LOCKFILE, *changelog)


@dataclass(frozen=True)
class UndoResult:
    plan: UndoPlan
    noop: bool = False
    cancelled: bool = False


Confirm = t.Callable[[UndoResult], bool]


def plan_undo(
    config: Config, repo: GitRepo, project: UvProject, root: Path
) -> UndoPlan:
    """
    Work out what the last release left behind, and refuse if it cannot go.

    Everything here is a read: the checks all run before the first write, so a
    published tag or an unreadable version leaves the project exactly as it was.
    """
    version = project.current_version()
    if not version:
        raise VommitError(f"No version found in {PYPROJECT}; there is nothing to undo.")

    git = config.git
    tag = _released_tag(repo, git.format_tag(version) if git else None)
    commit = _release_commit(repo, git.format_commit(version) if git else None)

    if tag and git and repo.remote_has_tag(git.origin, tag):
        raise VommitError(
            f"Tag '{tag}' has been pushed to '{git.origin}'; undoing it would "
            "rewrite history other people already have. Remove it there first."
        )
    if commit and (remotes := repo.remote_branches_containing("HEAD")):
        raise VommitError(
            f"The release commit is already on {', '.join(remotes)}; undoing it "
            "would rewrite published history. Revert it with a new commit instead."
        )

    previous_commit = "HEAD~1" if commit else "HEAD"
    previous = _previous_version(repo, project, previous_commit)
    if previous == version:
        raise VommitError(
            f"{PYPROJECT} already says {version} at {previous_commit}; "
            "there is no release here to undo."
        )

    return UndoPlan(
        version=version,
        previous=previous,
        tag=tag,
        commit=commit,
        previous_commit=previous_commit,
        changelog_path=_entry_to_remove(config, root, version) if not commit else None,
    )


def run_undo(
    config: Config,
    runner: Runner,
    root: Path,
    noop: bool = False,
    notify: Notify = lambda _: None,
    confirm: Confirm = always,
) -> UndoResult:
    """
    Take back the last release: drop the tag, the commit and the changes.

    A release that was committed is rewound, so the history keeps no trace of it.
    One that was only written to disk is reversed field by field, leaving any
    unrelated edits in those files alone.
    """
    repo = GitRepo(runner=runner, root=root)
    project = UvProject(runner=runner, root=root)

    plan = plan_undo(config, repo, project, root)
    result = UndoResult(plan=plan, noop=noop)
    if noop:
        return result
    if not confirm(result):
        return replace(result, noop=True, cancelled=True)

    if plan.tag:
        # first, so a failure halfway leaves the commit findable by its tag
        repo.delete_tag(plan.tag)
    if plan.rewinds_history:
        repo.reset_hard(t.cast(str, plan.previous_commit))
        notify(f"Rewound to {plan.previous}.")
        return result

    _restore_files(config, repo, project, root, plan)
    notify(f"Restored {plan.previous}.")
    return result


def _released_tag(repo: GitRepo, tag: str | None) -> str | None:
    """
    The release tag, but only if it is really on the commit we are looking at.
    """
    if not tag:
        return None
    return tag if repo.tag_commit(tag) == repo.head_commit() else None


def _release_commit(repo: GitRepo, expected_subject: str | None) -> str | None:
    """
    HEAD's subject, if it is the message a release commit would have carried.
    """
    if not expected_subject:
        return None
    subject = repo.head_subject()
    return subject if subject == expected_subject else None


def _previous_version(repo: GitRepo, project: UvProject, ref: str) -> str:
    content = repo.file_at(ref, PYPROJECT)
    version = project.version_in(content) if content else None
    if not version:
        raise VommitError(
            f"Could not read the version from {PYPROJECT} at {ref}, "
            "so there is nothing to go back to."
        )
    return version


def _entry_to_remove(config: Config, root: Path, version: str) -> Path | None:
    """
    The changelog holding an entry for `version`, if one was written at all.

    A prerelease under the default settings never got an entry, so there is
    nothing to take out of it.
    """
    settings = config.active_changelog
    if not settings:
        return None
    changelog = Changelog(settings=settings, root=root)
    if not changelog.path.exists():
        return None
    return changelog.path if changelog.remove(changelog.read(), version) else None


def _restore_files(
    config: Config,
    repo: GitRepo,
    project: UvProject,
    root: Path,
    plan: UndoPlan,
) -> None:
    """
    Reverse the writes a bump made, without reaching for the whole file.

    `uv version` rewrites the version field and relocks, so `uv.lock` follows
    along; the changelog entry is cut out by the changelog's own matcher.
    """
    if plan.changelog_path and (settings := config.active_changelog):
        changelog = Changelog(settings=settings, root=root)
        without = changelog.remove(changelog.read(), plan.version)
        if without is not None:
            plan.changelog_path.write_text(without)

    project.apply_set(plan.previous)
    # bump staged what it wrote, so undoing it has to unstage them again
    repo.unstage(plan.paths)
