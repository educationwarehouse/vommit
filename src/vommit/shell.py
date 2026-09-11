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


#: Substrings seen in the wild that mean "this process is now waiting on a
#: browser click or a one-time password nobody here can provide", so far only
#: from npm/bun's own publish flow. Neither is blocked on stdin (closing it,
#: below, stops a plain "press enter" prompt but not a poll against npm's own
#: servers for that browser click to have happened), so the only way to catch
#: either is to watch for their own wording as it is produced and abort right
#: there. Add to this tuple as other tools' equivalents turn up, rather than
#: try to catch the general shape some other way.
AUTH_PROMPT_PATTERNS: tuple[str, ...] = (
    "Authenticate your account at",
    "Enter one-time password",
    "one-time password:",
)

AUTH_PROMPT_MESSAGE = (
    "Aborted: this command is waiting on interactive authentication (a "
    "browser login or a one-time password), which cannot be answered here."
)


def _contains_auth_prompt(text: str) -> bool:
    return any(pattern in text for pattern in AUTH_PROMPT_PATTERNS)


class _AuthPromptSeen(Exception):
    """
    Raised by `_AuthPromptWatcher` to make `invoke` kill the subprocess the
    moment `AUTH_PROMPT_PATTERNS` appears in its output, rather than leave it
    to hang (or, at best, eventually time out on its own).
    """


class _AuthPromptWatcher(StreamWatcher):
    def __init__(self) -> None:
        self._raised = False

    def submit(self, stream: str) -> t.Iterable[str]:
        if not self._raised and _contains_auth_prompt(stream):
            self._raised = True
            raise _AuthPromptSeen(AUTH_PROMPT_MESSAGE)
        return []


class LocalRunner:
    """
    Runs commands directly via subprocess, without a shell.

    Every command run through here (`git`, `uv version`, a configured
    build/publish command) is meant to run unattended, so anything that turns
    out to need a human after all should fail immediately and visibly rather
    than block forever on an action nobody here can see was ever asked for.
    Two different blocking shapes need two different answers:

    - stdin is closed (`DEVNULL`) rather than inherited, so a prompt that is
      genuinely waiting on a keypress (a plain confirmation, say) gets
      immediate EOF instead of a real terminal to (maybe) block on;
    - output is read incrementally rather than only once the process exits,
      specifically so `AUTH_PROMPT_PATTERNS` can be caught and the process
      killed the moment one appears, because that shape of prompt is not
      blocked on stdin at all: it is polling a remote server for a browser
      click to have happened, and closing stdin does not stop that.

    A command that genuinely needs a terminal, like opening an editor, is
    deliberately never run through `Runner` at all; see `editor.py`.
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
            text=True,
            env={**os.environ, **env} if env else None,
        )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        aborted = threading.Event()

        def _drain(stream: t.IO[str], chunks: list[str]) -> None:
            for line in iter(stream.readline, ""):
                chunks.append(line)
                if not aborted.is_set() and _contains_auth_prompt(line):
                    aborted.set()
                    process.kill()
            stream.close()

        assert process.stdout is not None  # PIPE above guarantees this
        assert process.stderr is not None  # same
        readers = [
            threading.Thread(target=_drain, args=(process.stdout, stdout_chunks)),
            threading.Thread(target=_drain, args=(process.stderr, stderr_chunks)),
        ]
        for reader in readers:
            reader.start()
        process.wait()
        for reader in readers:
            reader.join()

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if aborted.is_set():
            stderr = (
                f"{stderr}\n{AUTH_PROMPT_MESSAGE}" if stderr else AUTH_PROMPT_MESSAGE
            )
        return CommandResult(
            command=command,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )


class ContextRunner:
    """
    Adapts an ewok/invoke Context, so tasks reuse the CLI's own runner config.

    Gives the same guarantee `LocalRunner` does, using `invoke`'s own
    mechanisms instead of reimplementing them: `in_stream=False` closes the
    child's stdin (a plain "press enter" prompt gets immediate EOF rather
    than a real terminal), and `_AuthPromptWatcher` aborts the command the
    moment `AUTH_PROMPT_PATTERNS` appears in its output (the shape of prompt
    that is not blocked on stdin at all, so closing it does nothing).
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
                watchers=[_AuthPromptWatcher()],
                env=env or {},
            )
        except ThreadException as error:
            for wrapped in error.exceptions:
                if isinstance(wrapped.value, _AuthPromptSeen):
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
