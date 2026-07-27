import shlex
import textwrap
import typing as t
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import tomlkit

from src.vommit.shell import CommandResult, LocalRunner

PYPROJECT_TEMPLATE = """
[project]
name = "sandbox"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = []

[build-system]
requires = ["uv_build>=0.11.3,<0.12.0"]
build-backend = "uv_build"

[tool.vommit.git]
enabled = true
origin = "origin"
branch = "{branch}"
on_wrong_branch = "error"
tag_format = "v{{version}}"
commit_format = "{{version}}"

[tool.vommit.changelog]
enabled = true
file = "CHANGELOG.md"
"""


@dataclass
class FakeRunner:
    """
    Answers commands by substring, so a test can fail exactly one git call.
    """

    responses: dict[str, CommandResult] = field(default_factory=dict)
    default: CommandResult = CommandResult("", 0, "", "")
    calls: list[str] = field(default_factory=list)

    def reply(
        self,
        contains: str,
        stdout: str = "",
        returncode: int = 0,
        stderr: str = "",
    ) -> "FakeRunner":
        self.responses[contains] = CommandResult(contains, returncode, stdout, stderr)
        return self

    def run(self, command: str) -> CommandResult:
        self.calls.append(command)
        for needle, result in self.responses.items():
            if needle in command:
                return result
        return self.default

    def ran(self, needle: str) -> bool:
        return any(needle in call for call in self.calls)


class Sandbox:
    """
    A real git project with a real remote; slow-ish, but these are the paths
    where a wrong assumption about git or uv actually bites.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.work = root / "work"
        self.remote = root / "remote.git"
        self.runner = LocalRunner()

    def git(self, *args: str, cwd: Path | None = None) -> CommandResult:
        return self.runner.run(shlex.join(["git", "-C", str(cwd or self.work), *args]))

    def create(self, branch: str = "main", config_branch: str | None = None) -> None:
        self.remote.mkdir(parents=True)
        self.work.mkdir(parents=True)
        self.runner.run(
            shlex.join(
                [
                    "git",
                    "init",
                    "--bare",
                    f"--initial-branch={branch}",
                    str(self.remote),
                ]
            )
        )
        self.runner.run(
            shlex.join(["git", "init", f"--initial-branch={branch}", str(self.work)])
        )
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")

        self.write(
            "pyproject.toml", PYPROJECT_TEMPLATE.format(branch=config_branch or branch)
        )
        self.write("README.md", "sandbox\n")
        self.write("src/sandbox/__init__.py", "")
        self.commit("chore: init")

        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-u", "origin", branch)

    def set_config(self, section: str, **values: t.Any) -> None:
        """
        Add or overwrite keys in a (dotted) `pyproject.toml` section.
        """
        path = self.work / "pyproject.toml"
        document = tomlkit.parse(path.read_text())
        node: t.Any = document
        for part in section.split("."):
            if part not in node:
                node[part] = tomlkit.table()
            node = node[part]
        for key, value in values.items():
            node[key] = value
        path.write_text(tomlkit.dumps(document))
        # committed right away: a bump refuses to run on a dirty pyproject.toml
        self.commit("chore: configure")

    def write(self, relative: str, content: str) -> Path:
        path = self.work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content))
        return path

    def commit(self, message: str, empty: bool = False) -> None:
        if not empty:
            self.git("add", "-A")
        self.git("commit", *(["--allow-empty"] if empty else []), "-m", message)

    def commits(self, *messages: str) -> None:
        for message in messages:
            self.commit(message, empty=True)

    def tags(self) -> list[str]:
        return self.git("tag", "-l").out.splitlines()

    def log(self, count: int = 5) -> list[str]:
        return self.git("log", f"-{count}", "--format=%s").out.splitlines()

    def status(self) -> list[str]:
        return self.git("status", "--porcelain").out.splitlines()

    def push_upstream(self, message: str, branch: str = "main") -> None:
        """
        Land a commit on the remote that `work` does not have yet.
        """
        other = self.root / "other"
        if not other.exists():
            self.runner.run(shlex.join(["git", "clone", str(self.remote), str(other)]))
            self.git("config", "user.name", "other", cwd=other)
            self.git("config", "user.email", "other@example.com", cwd=other)
        self.git("checkout", "-B", branch, f"origin/{branch}", cwd=other)
        self.git("commit", "--allow-empty", "-m", message, cwd=other)
        self.git("push", "origin", branch, cwd=other)


@pytest.fixture
def sandbox(tmp_path: Path) -> t.Iterator[Sandbox]:
    box = Sandbox(tmp_path)
    box.create()
    yield box
