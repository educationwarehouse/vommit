import re
import shlex
import subprocess
import typing as t
from pathlib import Path

from configuraptor import TypedConfig

TOML_KEY = "tool.vommit"
DERIVE_BRANCH = "<head>"

RE_HEAD = re.compile(r"refs/heads/(\S+)")


def bash(command: str) -> tuple[int, str, str]:
    pipes = subprocess.Popen(
        shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    return pipes.returncode, pipes.stdout.read().decode(), pipes.stderr.read().decode()


def throw(error: Exception) -> t.Never:
    """
    Functional raise, useful for if ... else ... or callbacks.
    """
    raise error


def find_main_branch_upstream(origin: str = "origin") -> str | None:
    origin = shlex.quote(origin)
    _, stdout, _ = bash(f"git ls-remote --symref {origin} HEAD")

    match = RE_HEAD.search(stdout)
    return match.group(1) if match else None


def find_main_branch_local():
    _, stdout, _ = bash("git config --get init.defaultBranch")
    return stdout.strip()


def find_main_branch(
    origin: str, remote: bool = True, local: bool = True
) -> str | None:
    return (
        (remote and find_main_branch_upstream(origin))
        or (local and find_main_branch_local())
        or None
    )


class WithDefault(TypedConfig):
    @classmethod
    def default(cls) -> t.Self:
        return cls.load()


class GitConfig(WithDefault):
    enabled: bool = True
    origin: str = "origin"
    branch: str | t.Literal["<head>", "<current>"] = (
        DERIVE_BRANCH  # special '<head>' does lookup (first remote, then local config)
    )
    on_wrong_branch: t.Literal["error", "warn", "switch"] = "error"

    # set to None to disable tagging/committing:
    tag_format: str | None = "v{version}"
    commit_format: str | None = "Release {version}"

    def __post_init__(self):
        if self.branch == DERIVE_BRANCH:
            self.branch = find_main_branch(
                self.origin, remote=True, local=True
            ) or throw(
                ValueError(
                    "main git branch could not be derived; Please set it via `tool.vommit.git.branch`"
                )
            )


class ChangelogConfig(WithDefault):
    levels: dict[str, str] = {
        # note: use {} syntax for pluralization
        "feat": "Feature{s}",
        "fix": "Bug Fix{es}",
        "perf": "Performance",
        "docs": "Documentation",
    }

    placeholder_regex: str = r"<!--next-version-placeholder-->"

    def __post_init__(self):
        self._placeholder_re = re.compile(self.placeholder_regex)


class PypiConfig(WithDefault):
    enabled: bool = True
    username: str = "__token__"
    use_keyring: bool = True


class CommandConfig(WithDefault):
    clean: str = "rm -r ./dist"
    build: str = "uv build"
    publish: str = "uv publish"

    release: str = "{clean} && {build} && {publish}"


class Config(WithDefault):
    git: GitConfig = GitConfig.default()
    changelog: ChangelogConfig = ChangelogConfig.default()
    pypi: PypiConfig = PypiConfig.default()
    commands: CommandConfig = CommandConfig.default()

    # feat!(scope): ... to bump major
    allow_breaking_bang: bool = True
    # todo:
    #  - compatibility/migration from old `tool.semantic_release` config

    version_bump_map: dict[str, t.Literal["major", "minor", "patch"]] = {
        "feat": "minor",
        "fix": "patch",
        "perf": "patch",
        "docs": "patch",
    }

    @classmethod
    def from_pyproject(cls, toml_key: str = TOML_KEY) -> t.Self:
        pyproject = Path.cwd() / "pyproject.toml"

        if not pyproject.exists():
            return cls.default()

        return cls.load(pyproject.absolute(), key=toml_key)
