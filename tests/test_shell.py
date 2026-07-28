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
    assert context.kwargs == {
        "command": "git status",
        "hide": True,
        "warn": True,
        "env": {},
    }


def test_context_runner_passes_env_through():
    context = FakeContext(FakeInvokeResult(0, "", ""))

    result = ContextRunner(context).run("uv publish", env={"TOKEN": "secret"})

    assert context.kwargs["env"] == {"TOKEN": "secret"}
    assert "secret" not in repr(result)
