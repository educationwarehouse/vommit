import re
import shlex
import string
import subprocess
import typing as t
from pathlib import Path

from configuraptor import Defaultable, TypedConfig

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


class GitConfig(TypedConfig, Defaultable):
    enabled: bool = True
    origin: str = "origin"

    # special '<head>' does lookup (first remote, then local config)
    branch: str | t.Literal["<head>", "<current>"] = DERIVE_BRANCH
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


class ChangelogConfig(TypedConfig, Defaultable):
    levels: dict[str, str] = {
        # note: use {} syntax for pluralization
        "feat": "Feature{s}",
        "fix": "Bug Fix{es}",
        "perf": "Performance",
        "docs": "Documentation",
    }

    placeholder_regex: str = r"^<!--\s*next-version-placeholder\s*-->$"

    def __post_init__(self):
        self._placeholder_re = re.compile(self.placeholder_regex, flags=re.MULTILINE)

    def apply_placeholder(self, changelog: str, to_insert: str) -> str | None:
        # Keep marker and insert content right below it.
        match = self._placeholder_re.search(changelog)
        if not match:
            return None

        marker = match.group(0)
        insertion = to_insert.strip("\n")
        replacement = f"{marker}\n\n{insertion}"

        return self._placeholder_re.sub(replacement, changelog, count=1)

    def pluralize(self, level: str, count: int) -> str:
        template = self.levels[level]

        # Placeholder strategy:
        # - singular (count == 1): remove suffix placeholders
        # - plural (count != 1): use placeholder name as suffix (e.g. {s}->{s}, {es}->{es})
        formatter = string.Formatter()
        field_names = {
            field_name
            for _, field_name, _, _ in formatter.parse(template)
            if field_name
        }
        forms = {name: (name if count != 1 else "") for name in field_names}
        return template.format(**forms)


class PypiConfig(TypedConfig, Defaultable):
    enabled: bool = True
    username: str = "__token__"
    use_keyring: bool = True


class CommandConfig(TypedConfig, Defaultable):
    clean: str = "rm -r ./dist"
    build: str = "uv build"
    publish: str = "uv publish"

    release: str = "{clean} && {build} && {publish}"


class Config(TypedConfig, Defaultable):
    git: GitConfig
    changelog: ChangelogConfig
    pypi: PypiConfig
    commands: CommandConfig

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
