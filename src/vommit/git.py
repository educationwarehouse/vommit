import dataclasses as dc
import re
import shlex
import typing as t
from pathlib import Path

from .commits import split_commit_log
from .config import CURRENT_BRANCH, DERIVE_BRANCH, GitConfig
from .errors import VommitError
from .shell import CommandResult, LocalRunner, Runner
from .versioning import is_prerelease

RE_HEAD = re.compile(r"refs/heads/(\S+)")

# what `git commit --author` accepts: a name, then an address in angle brackets
RE_AUTHOR = re.compile(r"^[^<>]+<[^<>]*>$")


def resolve_author(author: str | None) -> str | None:
    """
    `git.commit_author` as `--author` takes it, or None to leave it to git.

    Checked before the bump writes anything: git rejects a malformed author at
    commit time, which is after the version has been changed and staged.
    """
    author = (author or "").strip()
    if not author:
        return None
    if not RE_AUTHOR.fullmatch(author):
        raise VommitError(
            f"git.commit_author is {author!r}; git takes 'Name <email>'. "
            "Leave it empty to commit as yourself."
        )
    return author


@dc.dataclass(frozen=True)
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

    def _symbolic_branch(self) -> str | None:
        """
        The branch HEAD names, or None when HEAD names a commit directly.

        Answers on an unborn branch, where `rev-parse --abbrev-ref` fails
        outright, and declines on a detached checkout, where it does not.
        """
        result = self._git("symbolic-ref", "--short", "HEAD")
        return result.out if result.ok and result.out else None

    def current_branch(self) -> str:
        """
        The checked-out branch, also before the first commit.

        Falls back to `rev-parse`, which answers 'HEAD' on the detached checkout
        that CI hands us, the one state a symbolic ref cannot describe.
        """
        if branch := self._symbolic_branch():
            return branch
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

    def detect_release_branch(self, origin: str = "origin") -> str | None:
        """
        The branch to write into a fresh config, best answer first.

        The remote's HEAD comes first because it is the only authoritative
        answer to "which branch does this project release from"; asking the
        checkout first would pin a feature branch for anyone who ran `setup`
        from one. It is the checked-out branch that saves the case a remote
        cannot: a repository created moments ago, where `init.defaultBranch` is
        a guess about a branch that already has a name.
        """
        return (
            self.main_branch_upstream(origin)
            or self._symbolic_branch()
            or self.main_branch_local()
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

    def ensure_branch(
        self,
        git: GitConfig,
        on_message: t.Callable[[str], None],
        allow_branch: bool = False,
    ) -> str:
        """
        Make sure we are on the release branch; returns the branch we end up on.

        `allow_branch` is the per-run answer to a configured `error`: it stays
        on the branch that is checked out and only says so, which is what a
        prerelease cut from a feature branch needs. It deliberately does not
        turn into a `switch`: a run that was told to accept this branch has no
        business moving the checkout to another one.
        """
        target = self.resolve_branch(git)
        current = self.current_branch()
        if current == target:
            return target

        if allow_branch:
            on_message(f"On branch '{current}', expected '{target}'; continuing.")
            return current

        if git.on_wrong_branch == "switch":
            self._checked("checkout", target, action=f"switch to branch '{target}'")
            on_message(f"Switched to branch '{target}'.")
            return target

        if git.on_wrong_branch == "warn":
            on_message(f"On branch '{current}', expected '{target}'.")
            return current

        raise VommitError(
            f"On branch '{current}', expected '{target}'. "
            "Switch branches, pass --allow-branch to release from this one, "
            "or set `on_wrong_branch` to 'warn'/'switch'."
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
        if git.origin not in self.remotes():
            # nothing to be behind, and fetching would fail on the missing name
            on_message(f"No remote '{git.origin}'; skipping the freshness check.")
            return

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

    def tags_reachable(self, glob: str | None) -> list[str]:
        """
        Tags on this history, newest first.

        Ordered by the date git recorded for the tag rather than by the shape of
        the history: `git tag` cannot sort topologically, and for the linear
        release history this is used on the two agree.
        """
        match = ["--list", glob] if glob else []
        result = self._git("tag", "--merged", "HEAD", "--sort=-creatordate", *match)
        return result.out.splitlines() if result.ok else []

    def last_stable_tag(
        self,
        glob: str | None,
        to_version: t.Callable[[str], str | None],
    ) -> str | None:
        """
        Most recent tag holding a finished release, skipping prereleases.

        This is the changelog's starting point when prereleases are aggregated:
        everything since the last real release belongs in the next entry.
        """
        for tag in self.tags_reachable(glob):
            version = to_version(tag)
            if version and not is_prerelease(version):
                return tag
        return None

    def commit_messages_since(self, since: str | None) -> list[str]:
        commit_range = [f"{since}..HEAD"] if since else []
        result = self._git("log", "-z", "--no-merges", "--format=%B", *commit_range)
        if not result.ok:
            # no commits at all yet
            return []
        return split_commit_log(result.stdout)

    def subject(self, ref: str = "HEAD") -> str:
        """
        The first line of `ref`'s commit message, for naming it to a human.
        """
        return self._checked(
            "log", "-1", "--format=%s", ref, action=f"read the message of '{ref}'"
        ).out

    def head_subject(self) -> str:
        return self.subject()

    def tag_commit(self, tag: str) -> str | None:
        """
        The commit a tag resolves to, or None if the tag does not exist.
        """
        result = self._git(
            "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"
        )
        return result.out if result.ok else None

    def head_commit(self) -> str:
        return self._checked("rev-parse", "HEAD", action="resolve HEAD").out

    def remote_has_tag(self, origin: str, tag: str) -> bool:
        """
        Whether `origin` already carries this tag.

        A network call, so only worth making when something is about to be
        undone: a published tag is one other people may already have fetched.
        """
        result = self._git("ls-remote", "--tags", origin, f"refs/tags/{tag}")
        return bool(result.ok and result.out)

    def remote_branches_containing(self, ref: str) -> list[str]:
        """
        Remote-tracking branches that already include `ref`, per the last fetch.
        """
        result = self._git("branch", "--remotes", "--contains", ref)
        if not result.ok:
            return []
        return [
            name
            for line in result.stdout.splitlines()
            # skip the symbolic 'origin/HEAD -> origin/main' entry, which names
            # a branch that is listed on its own anyway
            if (name := line.strip()) and "->" not in name
        ]

    def delete_tag(self, name: str) -> None:
        self._checked("tag", "--delete", name, action=f"delete tag '{name}'")

    def reset_hard(self, ref: str) -> None:
        self._checked("reset", "--hard", ref, action=f"reset to '{ref}'")

    def file_at(self, ref: str, path: str) -> str | None:
        """
        The contents of `path` as of `ref`, or None if it was not there.
        """
        result = self._git("show", f"{ref}:{path}")
        return result.stdout if result.ok else None

    def _changed(self) -> set[str]:
        result = self._checked(
            "status", "--porcelain", action="inspect the working tree"
        )
        return {line[3:].strip() for line in result.stdout.splitlines() if line}

    def dirty_paths(self, paths: t.Iterable[str]) -> list[str]:
        """
        Which of `paths` already have uncommitted changes.
        """
        changed = self._changed()
        return sorted(path for path in paths if path in changed)

    def uncommitted(self) -> list[str]:
        """
        Every path the working tree has changed, whether tracked or not.

        Wider than `dirty_paths` on purpose: a release builds the tree it is
        standing in, so an untracked source file ends up in the artifact just
        as surely as a modified one does. Ignored files stay out, since git
        does not report them and a build would not pick them up either.
        """
        return sorted(self._changed())

    def ignores(self, path: str) -> bool:
        """
        Whether Git ignores an otherwise untracked path.

        `check-ignore` exits with one when a path is not ignored, so that is a
        normal answer rather than a failed Git operation.
        """
        result = self._git("check-ignore", "--quiet", "--", path)
        if result.returncode == 0:
            return True
        elif result.returncode == 1:
            return False
        else:  # pragma: no cover - Git reserves these codes for fatal errors
            raise VommitError(
                f"Could not check whether {path} is ignored: {result.error}"
            )

    def add(self, paths: t.Iterable[str]) -> None:
        targets = list(paths)
        if targets:
            self._checked("add", "--", *targets, action="stage the release changes")

    def unstage(self, paths: t.Iterable[str]) -> None:
        """
        Drop `paths` from the index, leaving the working tree as it is.

        A file the last commit does not have simply becomes untracked again,
        which is the honest state for something a release created.
        """
        targets = list(paths)
        if targets:
            self._checked("reset", "--quiet", "--", *targets, action="unstage")

    def commit(self, message: str, author: str | None = None) -> None:
        extra = [f"--author={author}"] if author else []
        self._checked(
            "commit", "-m", message, *extra, action="create the release commit"
        )

    def ensure_tag_available(self, name: str) -> None:
        if self.ref_exists(f"refs/tags/{name}"):
            raise VommitError(f"Tag '{name}' already exists.")

    def tag(self, name: str) -> None:
        self.ensure_tag_available(name)
        self._checked("tag", name, action=f"create tag '{name}'")

    def push(self, origin: str, branch: str) -> None:
        self._checked("push", origin, branch, action=f"push '{branch}' to '{origin}'")

    def is_repo_root(self) -> bool:
        """
        Whether this directory is a repository of its own.

        `uv init` skips `git init` inside an existing repository, which leaves a
        new project sharing its parent's history: the toplevel then answers, but
        it is somebody else's.
        """
        result = self._git("rev-parse", "--show-toplevel")
        return result.ok and Path(result.out).resolve() == self.root.resolve()

    def rename_branch(self, branch: str) -> None:
        """
        Rename the checked-out branch, which also works before the first commit.
        """
        self._checked("branch", "-m", branch, action=f"rename the branch to '{branch}'")

    def remotes(self) -> list[str]:
        """
        The remotes this repository knows, by name.

        Empty outside a repository as well as inside one without remotes: both
        mean there is nothing to fetch from, which is all a caller asks.
        """
        result = self._git("remote")
        return result.out.splitlines() if result.ok else []

    def add_remote(self, name: str, url: str) -> None:
        """
        Point `name` at `url`, failing when the repository already has that name.
        """
        self._checked("remote", "add", name, url, action=f"add remote '{name}'")

    def push_upstream(self, origin: str, branch: str) -> None:
        """
        Push and record the upstream, so later runs can compare against it.
        """
        self._checked(
            "push",
            "--set-upstream",
            origin,
            branch,
            action=f"push '{branch}' to '{origin}'",
        )

    def push_tag(self, origin: str, tag: str) -> None:
        """
        Push one tag, by ref.

        Not `--follow-tags`, which silently skips lightweight tags and ours are
        lightweight, and not `--tags`, which would push every tag lying around
        the repository. Naming the ref also means a failure here is reported as
        the tag failing rather than the branch.
        """
        self._checked(
            "push",
            origin,
            f"refs/tags/{tag}",
            action=f"push tag '{tag}' to '{origin}'",
        )


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
