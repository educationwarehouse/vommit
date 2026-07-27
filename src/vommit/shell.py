import shlex
import subprocess
import typing as t
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
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
    implementations below (and fakes in tests) agree on quoting.
    """

    def run(self, command: str) -> CommandResult: ...  # pragma: no cover


class LocalRunner:
    """
    Runs commands directly via subprocess, without a shell.
    """

    def run(self, command: str) -> CommandResult:
        completed = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            check=False,
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

    def __init__(self, c: t.Any) -> None:
        self._context = c

    def run(self, command: str) -> CommandResult:
        result = self._context.run(command, hide=True, warn=True)
        return CommandResult(
            command=command,
            returncode=result.exited,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def bash(command: str) -> CommandResult:
    """
    Convenience one-off for code that has no runner to hand.
    """
    return LocalRunner().run(command)
