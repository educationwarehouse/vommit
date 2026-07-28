import dataclasses as dc
import os
import shlex
import subprocess
import typing as t

from ewok import Context


@dc.dataclass(frozen=True)
class CommandResult:
    """
    What a command did. Deliberately without the environment it ran in: `env`
    carries the PyPI token, and a result gets printed, logged and asserted on.
    """

    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()

    @property
    def error(self) -> str:
        """
        Best available explanation of a failure; some tools report on stdout.
        """
        return self.stderr.strip() or self.stdout.strip() or f"exit {self.returncode}"


class Runner(t.Protocol):
    """
    Anything that can execute a shell command and report on it.

    Commands are passed as a single string built with `shlex.join`, so both
    implementations below (and fakes in tests) agree on quoting. `env` is added
    to the environment the command inherits, never replacing it.
    """

    def run(
        self,
        command: str,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...  # pragma: no cover


class LocalRunner:
    """
    Runs commands directly via subprocess, without a shell.
    """

    def run(
        self,
        command: str,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **env} if env else None,
        )
        return CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class ContextRunner:
    """
    Adapts an ewok/invoke Context, so tasks reuse the CLI's own runner config.
    """

    def __init__(self, ctx: Context) -> None:
        self._context = ctx

    def run(
        self,
        command: str,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        # invoke merges `env` into the inherited environment unless asked not to
        result = self._context.run(command, hide=True, warn=True, env=env or {})
        return CommandResult(
            command=command,
            returncode=result.exited,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def shell(command: str) -> str:
    """
    `command` wrapped so that shell syntax survives either runner.

    `LocalRunner` splits with `shlex` and never sees a shell, so `&&`, pipes and
    globs would otherwise reach the first program as literal arguments. Handing
    the whole line to `bash -c` is what makes a configured command mean the same
    thing everywhere; `ContextRunner` just nests one shell inside another.
    """
    return shlex.join(["bash", "-c", command])


def bash(command: str) -> CommandResult:
    """
    Convenience one-off for code that has no runner to hand.
    """
    return LocalRunner().run(command)
