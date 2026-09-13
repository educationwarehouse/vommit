import time

import invoke
import pytest
from invoke.exceptions import ThreadException
from invoke.util import ExceptionWrapper

from src.vommit.shell import CommandResult, ContextRunner, LocalRunner, bash, shell


class FakeInvokeResult:
    def __init__(self, exited: int, stdout: str, stderr: str) -> None:
        self.exited = exited
        self.stdout = stdout
        self.stderr = stderr


class FakeContext:
    def __init__(self, result: FakeInvokeResult) -> None:
        self.result = result
        self.kwargs: dict = {}

    def run(self, command: str, **kwargs):
        self.kwargs = {"command": command, **kwargs}
        return self.result


def test_command_result_reports_success():
    result = CommandResult("true", 0, "  out  \n", "")

    assert result.ok is True
    assert result.out == "out"


def test_command_result_error_prefers_stderr_then_stdout():
    assert CommandResult("x", 1, "out", "err").error == "err"
    assert CommandResult("x", 1, "out", "  ").error == "out"
    assert CommandResult("x", 3, "", "").error == "exit 3"


def test_local_runner_captures_the_exit_code():
    # regression: the old subprocess helper never waited, so it always
    # reported a returncode of None.
    ok = LocalRunner().run("git --version")
    assert ok.ok is True
    assert ok.returncode == 0
    assert "git version" in ok.stdout

    failed = LocalRunner().run("git rev-parse --verify --quiet nope-nope-nope")
    assert failed.ok is False
    assert failed.returncode != 0


def test_local_runner_closes_stdin_instead_of_inheriting_it():
    """
    Regression: stdin used to inherit the caller's, so a configured command
    needing a live terminal blocked forever instead of failing, and behind a
    captured runner it blocked invisibly. `cat` with no input is that shape:
    it blocks until stdin closes, so this hangs the whole suite on a version
    of this method that inherits stdin.
    """
    result = LocalRunner().run("cat")
    assert result.ok is True
    assert result.stdout == ""


def test_local_runner_kills_a_command_asking_for_interactive_auth():
    """
    Regression: closing stdin (above) stops a plain "press enter" prompt from
    blocking forever, but not this shape of prompt, which is not blocked on
    stdin at all: it is polling a remote server for a browser click to have
    happened. `printf` stands in for a real publish command that prints
    an auth prompt then would otherwise sit forever (`sleep 30`, here) doing
    that polling; if the pattern is not caught, this test takes 30 seconds
    and still passes, so the wall-clock time is the real assertion.
    """
    start = time.monotonic()
    result = LocalRunner().run(
        shell("printf 'Authenticate your account at https://example.com\\n'; sleep 30")
    )
    elapsed = time.monotonic() - start

    assert elapsed < 5, f"took {elapsed:.1f}s, the sleep was not interrupted"
    assert result.ok is False
    assert "interactive authentication" in result.stderr


def test_local_runner_respects_shell_quoting():
    result = LocalRunner().run("git -c user.name='two words' config user.name")
    assert result.out == "two words"


def test_bash_convenience():
    assert bash("git --version").ok is True


def test_shell_survives_the_split_that_local_runner_does():
    # the whole point: `&&` reaches bash instead of becoming an argument to echo
    result = LocalRunner().run(shell("echo one && echo two"))

    assert result.out == "one\ntwo"


def test_shell_handles_quotes_in_the_wrapped_command():
    assert LocalRunner().run(shell("echo 'a b'  |  cat")).out == "a b"


def test_local_runner_adds_env_without_dropping_the_rest():
    result = LocalRunner().run(shell("echo $TOKEN-$PATH"), env={"TOKEN": "secret"})

    assert result.out.startswith("secret-/")


def test_local_runner_env_is_absent_from_the_result():
    # a CommandResult gets printed and asserted on; a token must not ride along
    result = LocalRunner().run(shell("true"), env={"TOKEN": "secret"})

    assert "secret" not in repr(result)


def test_context_runner_adapts_an_invoke_result():
    context = FakeContext(FakeInvokeResult(1, "out", "err"))

    result = ContextRunner(context).run("git status")

    assert result == CommandResult("git status", 1, "out", "err")
    kwargs = dict(context.kwargs)
    watchers = kwargs.pop("watchers")
    assert kwargs == {
        "command": "git status",
        "hide": True,
        "warn": True,
        "in_stream": False,
        "env": {},
    }
    assert len(watchers) == 1


def test_context_runner_passes_env_through():
    context = FakeContext(FakeInvokeResult(0, "", ""))

    result = ContextRunner(context).run("uv publish", env={"TOKEN": "secret"})

    assert context.kwargs["env"] == {"TOKEN": "secret"}
    assert "secret" not in repr(result)


def test_context_runner_kills_a_command_asking_for_interactive_auth():
    """
    The real `invoke.Context`, not `FakeContext`: `FakeContext` never runs a
    subprocess at all, so it cannot exercise the actual mechanism this relies
    on (an `invoke` watcher raising inside a background thread, `invoke`
    wrapping that in a `ThreadException`, and this method unwrapping it back
    out again) the way `test_local_runner_kills_...` exercises `LocalRunner`'s
    own version of the same guarantee.

    Prints ordinary output before the prompt, the way `bun publish` prints a
    packed-file summary before ever asking for authentication: the watcher
    has to see (and pass over) that harmless first line without raising,
    which a command that goes straight to the prompt would not exercise.
    """
    context = invoke.Context()
    start = time.monotonic()
    result = ContextRunner(context).run(
        shell(
            "printf 'packed 133B package.json\\n'; sleep 0.2; "
            "printf 'Authenticate your account at https://example.com\\n'; "
            "sleep 30"
        )
    )
    elapsed = time.monotonic() - start

    assert elapsed < 5, f"took {elapsed:.1f}s, the sleep was not interrupted"
    assert result.ok is False
    assert "interactive authentication" in result.stderr


def test_context_runner_reraises_an_unrelated_thread_exception():
    """
    Only the specific exception `AuthPromptWatcher` raises is meant to be
    caught and turned into a failed `CommandResult`; anything else a watcher
    (or `invoke` itself) raises in a background thread is a real bug and
    must not be swallowed the same way.
    """

    class ExplodingContext(FakeContext):
        def run(self, command, **kwargs):
            wrapped = ExceptionWrapper(
                kwargs={},
                type=RuntimeError,
                value=RuntimeError("not an auth prompt"),
                traceback=None,
            )
            raise ThreadException([wrapped])

    with pytest.raises(ThreadException):
        ContextRunner(ExplodingContext(FakeInvokeResult(0, "", ""))).run("echo hi")


def test_local_runner_catches_a_prompt_with_no_trailing_newline():
    """
    Regression: the drain used to read whole lines, so it could only ever see a
    pattern that arrived with a newline after it. A prompt is written *without*
    one, because the cursor has to stay on the line it is asking on, which made
    the two one-time-password patterns unreachable in exactly the situation
    they exist for. `printf` with no `\\n` reproduces it.
    """
    start = time.monotonic()
    result = LocalRunner().run(shell("printf 'Enter one-time password:'; sleep 30"))
    elapsed = time.monotonic() - start

    assert elapsed < 5, f"took {elapsed:.1f}s, the sleep was not interrupted"
    assert result.ok is False
    assert "interactive authentication" in result.stderr


def test_local_runner_catches_a_prompt_split_across_reads():
    """
    Output arrives in whatever sized pieces the process writes it, so a pattern
    can straddle two of them. The matcher keeps a window of recent bytes rather
    than testing each piece on its own.
    """
    result = LocalRunner().run(
        shell("printf 'Enter one-'; sleep 0.2; printf 'time password:'; sleep 30")
    )

    assert result.ok is False
    assert "interactive authentication" in result.stderr


def test_local_runner_survives_output_that_is_not_utf_8():
    """
    Regression: decoding used to happen in the reader thread under the strict
    codec, so one undecodable byte (a commit subject in some other encoding is
    the realistic source) killed the thread and silently truncated the result
    instead of raising anywhere visible.
    """
    result = LocalRunner().run(shell("printf 'caf\\351 x'"))

    assert result.ok is True
    assert result.out.startswith("caf")
    assert result.out.endswith("x")


def test_local_runner_does_not_abort_on_a_prompt_quoted_mid_line():
    """
    Regression: every command goes through a `Runner`, so an unanchored match
    killed `git log` over a commit subject quoting the wording. A tool asking
    the question starts a line with it; a message mentioning it does not.
    """
    quoted = "fix: do not Enter one-time password on publish"
    result = LocalRunner().run(shell(f"echo '{quoted}'"))

    assert result.ok is True
    assert result.out == quoted
    assert "interactive authentication" not in result.stderr


def test_local_runner_catches_an_indented_prompt():
    """
    A line start, not column zero: npm indents some of its output.
    """
    result = LocalRunner().run(shell("printf '   Enter one-time password:'; sleep 30"))

    assert result.ok is False
    assert "interactive authentication" in result.stderr
