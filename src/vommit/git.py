import re
import shlex
import typing as t
from dataclasses import dataclass
from pathlib import Path

from .commits import split_commit_log
from .config import CURRENT_BRANCH, DERIVE_BRANCH, GitConfig
from .errors import VommitError
from .shell import CommandResult, LocalRunner, Runner

RE_HEAD = re.compile(r"refs/heads/(\S+)")


@dataclass(frozen=True)
class GitRepo:
    """
    The git operations vommit needs, each one checked.

    Nothing here reports success for a command that failed; that is what let a
    tag get created on a commit that never happened.
    """

    runner: Runner
    root: Path

    def _git(self, *args: str) -> CommandResult:
        return self.runner.run(shlex.join(["git", "-C", str(self.root), *args]))

    def _checked(self, *args: str, action: str) -> CommandResult:
        result = self._git(*args)
        if not result.ok:
            raise VommitError(f"Could not {action}: {result.error}")
        return result

    def current_branch(self) -> str:
        return self._checked(
            "rev-parse", "--abbrev-ref", "HEAD", action="determine the current branch"
        ).out

    def main_branch_upstream(self, origin: str = "origin") -> str | None:
        result = self._git("ls-remote", "--symref", origin, "HEAD")
        match = RE_HEAD.search(result.stdout)
        return match.group(1) if match else None

    def main_branch_local(self) -> str:
        return self._git("config", "--get", "init.defaultBranch").out

    def main_branch(
        self,
        origin: str,
        remote: bool = True,
        local: bool = True,
    ) -> str | None:
        return (
            (remote and self.main_branch_upstream(origin))
            or (local and self.main_branch_local())
            or None
        )

    def resolve_branch(self, git: GitConfig) -> str:
        """
        The concrete branch name this project releases from.
        """
        if git.branch == CURRENT_BRANCH:
            return self.current_branch()
        if git.branch != DERIVE_BRANCH:
            return git.branch

        derived = self.main_branch(git.origin)
        if not derived:
            raise VommitError(
                "The main git branch could not be derived; "
                "please set it via `tool.vommit.git.branch`."
            )
        return derived

    def ensure_branch(self, git: GitConfig, on_message: t.Callable[[str], None]) -> str:
        """
        Make sure we are on the release branch; returns the branch we end up on.
        """
        target = self.resolve_branch(git)
        current = self.current_branch()
        if current == target:
            return target

        if git.on_wrong_branch == "switch":
            self._checked("checkout", target, action=f"switch to branch '{target}'")
            on_message(f"Switched to branch '{target}'.")
            return target

        if git.on_wrong_branch == "warn":
            on_message(f"On branch '{current}', expected '{target}'.")
            return current

        raise VommitError(
            f"On branch '{current}', expected '{target}'. "
            "Switch branches, or set `on_wrong_branch` to 'warn'/'switch'."
        )

    def ref_exists(self, ref: str) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", ref).ok

    def commits_behind(self, ref: str) -> int:
        return int(
            self._checked(
                "rev-list", f"HEAD..{ref}", "--count", action=f"compare HEAD to '{ref}'"
            ).out
            or 0
        )

    def ensure_up_to_date(
        self,
        git: GitConfig,
        branch: str,
        on_message: t.Callable[[str], None],
    ) -> None:
        """
        Refuse to release while the remote has commits we do not have.
        """
        self._checked("fetch", git.origin, action=f"fetch from '{git.origin}'")

        upstream = f"{git.origin}/{branch}"
        if not self.ref_exists(upstream):
            # a branch that was never pushed cannot be behind anything
            on_message(
                f"No upstream branch '{upstream}'; skipping the freshness check."
            )
            return

        behind = self.commits_behind(upstream)
        if behind:
            raise VommitError(
                f"Local branch is {behind} commit(s) behind '{upstream}'; "
                "pull before bumping."
            )

    def last_tag(self, glob: str | None) -> str | None:
        """
        Most recent tag reachable from HEAD, preferring ones this project made.
        """
        match = ["--match", glob] if glob else []
        result = self._git("describe", "--tags", "--abbrev=0", *match)
        if result.ok:
            return result.out

        if not glob:
            return None
        # tagging may have been reconfigured; fall back to any tag
        fallback = self._git("describe", "--tags", "--abbrev=0")
        return fallback.out if fallback.ok else None

    def commit_messages_since(self, since: str | None) -> list[str]:
        commit_range = [f"{since}..HEAD"] if since else []
        result = self._git("log", "-z", "--no-merges", "--format=%B", *commit_range)
        if not result.ok:
            # no commits at all yet
            return []
        return split_commit_log(result.stdout)

    def dirty_paths(self, paths: t.Iterable[str]) -> list[str]:
        """
        Which of `paths` already have uncommitted changes.
        """
        result = self._checked(
            "status", "--porcelain", action="inspect the working tree"
        )
        changed = {line[3:].strip() for line in result.stdout.splitlines() if line}
        return sorted(path for path in paths if path in changed)

    def add(self, paths: t.Iterable[str]) -> None:
        targets = list(paths)
        if targets:
            self._checked("add", "--", *targets, action="stage the release changes")

    def commit(self, message: str) -> None:
        self._checked("commit", "-m", message, action="create the release commit")

    def ensure_tag_available(self, name: str) -> None:
        if self.ref_exists(f"refs/tags/{name}"):
            raise VommitError(f"Tag '{name}' already exists.")

    def tag(self, name: str) -> None:
        self.ensure_tag_available(name)
        self._checked("tag", name, action=f"create tag '{name}'")


def here(root: str | Path | None = None) -> GitRepo:
    """
    A repo rooted at `root` (default: the working directory), run locally.
    """
    return GitRepo(runner=LocalRunner(), root=Path(root) if root else Path.cwd())


def find_main_branch_upstream(origin: str = "origin") -> str | None:
    return here().main_branch_upstream(origin)


def find_main_branch_local() -> str:
    return here().main_branch_local()


def find_main_branch(
    origin: str, remote: bool = True, local: bool = True
) -> str | None:
    return here().main_branch(origin, remote=remote, local=local)
