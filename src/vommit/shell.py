import dataclasses as dc
import os
import shlex
import subprocess
import threading
import typing as t

from ewok import Context
from invoke.exceptions import ThreadException
from invoke.watchers import StreamWatcher


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


# Wording that means the process is waiting on a browser click or a one-time
# password nobody here can provide. Neither is blocked on stdin, so closing it
# does not help; watching the output is the only way to catch them. Add to
# this tuple as other tools' equivalents turn up.
AUTH_PROMPT_PATTERNS: tuple[str, ...] = (
    "Authenticate your account at",
    "Enter one-time password",
    "one-time password:",
)

# How many recent bytes to match the patterns against, so one arriving in two
# reads is still seen.
MATCH_WINDOW = 256

AUTH_PROMPT_MESSAGE = (
    "Aborted: this command is waiting on interactive authentication (a "
    "browser login or a one-time password), which cannot be answered here."
)


def decode(raw: bytes) -> str:
    """
    Captured output as text, never refusing it: output is only matched and
    shown back in errors, so one undecodable byte should not cost the rest.
    """
    return raw.decode("utf-8", errors="replace")


def contains_auth_prompt(text: str) -> bool:
    return any(pattern in text for pattern in AUTH_PROMPT_PATTERNS)


class AuthPromptSeen(Exception):
    """
    Raised by `AuthPromptWatcher` to make invoke kill the subprocess rather
    than let it hang on a prompt nothing here can answer.
    """


class AuthPromptWatcher(StreamWatcher):
    def __init__(self) -> None:
        self._raised = False

    def submit(self, stream: str) -> t.Iterable[str]:
        if not self._raised and contains_auth_prompt(stream):
            self._raised = True
            raise AuthPromptSeen(AUTH_PROMPT_MESSAGE)
        return []


class LocalRunner:
    """
    Runs commands directly via subprocess, without a shell.

    Everything here runs unattended, so two blocking shapes need answering.
    Closing stdin (`DEVNULL`) gives a plain confirmation prompt immediate EOF
    instead of a terminal to wait on. An auth prompt is not blocked on stdin
    at all, though, so `Capture` watches the output and kills the process
    when one appears.

    A command that genuinely needs a terminal, like an editor, is never run
    through `Runner`; see `editor.py`.
    """

    def run(
        self,
        command: str,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        process = subprocess.Popen(
            shlex.split(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **env} if env else None,
        )
        assert process.stdout is not None  # PIPE above guarantees this
        assert process.stderr is not None  # same

        capture = Capture(process)
        stdout, stderr = capture.run(process.stdout, process.stderr)
        if capture.aborted:
            stderr = (
                f"{stderr}\n{AUTH_PROMPT_MESSAGE}" if stderr else AUTH_PROMPT_MESSAGE
            )
        return CommandResult(
            command=command,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )


class Capture:
    """
    Reads both pipes of a running process, killing it if an auth prompt shows
    up in either.

    One thread per pipe, because a single one would block on whichever stream
    the process writes to second. `os.read` rather than `readline`, because a
    prompt has no trailing newline to wait for: the cursor has to stay on the
    line it is asking on, so reading by line would hang on exactly the case
    this exists to catch.
    """

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self.aborted = False

    def run(self, stdout: t.IO[bytes], stderr: t.IO[bytes]) -> tuple[str, str]:
        captured: dict[t.IO[bytes], list[bytes]] = {stdout: [], stderr: []}
        readers = [
            threading.Thread(target=self._drain, args=(stream, chunks))
            for stream, chunks in captured.items()
        ]
        for reader in readers:
            reader.start()
        self._process.wait()
        for reader in readers:
            reader.join()
        return (
            decode(b"".join(captured[stdout])),
            decode(b"".join(captured[stderr])),
        )

    def _drain(self, stream: t.IO[bytes], chunks: list[bytes]) -> None:
        recent = b""
        while chunk := os.read(stream.fileno(), 8192):
            chunks.append(chunk)
            if self.aborted:
                continue
            # a window rather than the chunk, so a pattern arriving in two
            # reads is still seen
            recent = (recent + chunk)[-MATCH_WINDOW:]
            if contains_auth_prompt(decode(recent)):
                self.aborted = True
                self._process.kill()
        stream.close()


class ContextRunner:
    """
    Adapts an ewok/invoke Context, so tasks reuse the CLI's own runner config.

    Same guarantee as `LocalRunner`, through invoke's own mechanisms:
    `in_stream=False` closes the child's stdin, and `AuthPromptWatcher`
    aborts on a prompt that closing stdin would not stop.
    """

    def __init__(self, ctx: Context) -> None:
        self._context = ctx

    def run(
        self,
        command: str,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        try:
            # invoke merges `env` into the inherited environment unless asked
            # not to
            result = self._context.run(
                command,
                hide=True,
                warn=True,
                in_stream=False,
                watchers=[AuthPromptWatcher()],
                env=env or {},
            )
        except ThreadException as error:
            for wrapped in error.exceptions:
                if isinstance(wrapped.value, AuthPromptSeen):
                    return CommandResult(
                        command=command,
                        returncode=1,
                        stdout="",
                        stderr=str(wrapped.value),
                    )
            raise
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
