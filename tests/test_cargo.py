"""
A crate published by itself: what `setup` suggests, and the credential
`cargo publish` authenticates with.
"""

from pathlib import Path

import pytest

from src.vommit import cargo
from src.vommit.errors import VommitError

from .conftest import FakeRunner


@pytest.fixture(autouse=True)
def cargo_home(tmp_path, monkeypatch) -> Path:
    """
    A `CARGO_HOME` of the test's own, and no token in the environment, so
    nothing here reads (or `cargo logout` clears) the machine's real one.
    """
    home = tmp_path / "cargo-home"
    home.mkdir()
    monkeypatch.setenv("CARGO_HOME", str(home))
    monkeypatch.delenv(cargo.TOKEN_VAR, raising=False)
    return home


def store(home: Path, body: str, name: str = "credentials.toml") -> Path:
    path = home / name
    path.write_text(body)
    return path


def test_cargo_home_follows_the_environment(cargo_home):
    assert cargo.cargo_home() == cargo_home


def test_cargo_home_defaults_to_the_users_dot_cargo(monkeypatch):
    monkeypatch.delenv("CARGO_HOME")
    assert cargo.cargo_home() == Path.home() / ".cargo"


def test_suggested_commands_package_before_the_push_and_publish_after():
    commands = cargo.suggested_commands()

    assert commands.clean == ""
    assert commands.build == "cargo package"
    assert commands.publish == "cargo publish"
    assert commands.post_publish == ""


def test_a_workspace_packages_and_publishes_every_member():
    commands = cargo.suggested_commands(workspace=True)

    assert commands.build == "cargo package --workspace"
    assert commands.publish == "cargo publish --workspace"
    assert commands.clean == ""


def test_no_token_anywhere():
    assert cargo.token_source() is None
    assert cargo.has_token() is False


def test_a_token_in_the_environment(monkeypatch):
    monkeypatch.setenv(cargo.TOKEN_VAR, "cio_secret")

    assert cargo.token_source() == f"${cargo.TOKEN_VAR}"
    assert cargo.has_token() is True


def test_the_environment_wins_over_a_stored_token(cargo_home, monkeypatch):
    """
    That is the order cargo itself reads them in, so it is the one `--show`
    has to report.
    """
    store(cargo_home, '[registry]\ntoken = "cio_stored"\n')
    monkeypatch.setenv(cargo.TOKEN_VAR, "cio_secret")

    assert cargo.token_source() == f"${cargo.TOKEN_VAR}"


@pytest.mark.parametrize("name", ["credentials.toml", "credentials"])
def test_a_token_cargo_login_stored(cargo_home, name):
    path = store(cargo_home, '[registry]\ntoken = "cio_stored"\n', name)

    assert cargo.stored_token_file() == path
    assert cargo.token_source() == str(path)


@pytest.mark.parametrize(
    "body",
    [
        "",
        '[registries.other]\ntoken = "cio_elsewhere"\n',
        "registry = 1\n",
        '[registry]\ntoken = ""\n',
        "[registry\nnot toml",
    ],
)
def test_a_credentials_file_without_a_crates_io_token(cargo_home, body):
    store(cargo_home, body)
    assert cargo.stored_token_file() is None


def test_login_feeds_the_token_on_stdin_rather_than_the_command_line():
    runner = FakeRunner()

    cargo.login("cio_secret", runner)

    assert runner.ran("cargo login")
    assert "cio_secret" not in runner.calls[0]
    assert runner.env_for("cargo login") == {cargo.LOGIN_VAR: "cio_secret"}


def test_login_does_not_set_the_variable_cargo_reads_as_a_credential():
    runner = FakeRunner()
    cargo.login("cio_secret", runner)
    assert cargo.TOKEN_VAR not in (runner.env_for("cargo login") or {})


def test_a_failed_login_is_reported():
    runner = FakeRunner().reply("cargo login", returncode=101, stderr="bad token")

    with pytest.raises(VommitError, match="`cargo login` failed: bad token"):
        cargo.login("cio_secret", runner)


def test_logout_with_nothing_stored_runs_nothing(monkeypatch):
    # an environment token is not something `cargo logout` can take away
    monkeypatch.setenv(cargo.TOKEN_VAR, "cio_secret")
    runner = FakeRunner()

    assert cargo.logout(runner) is False
    assert runner.calls == []


def test_logout_removes_a_stored_token(cargo_home):
    store(cargo_home, '[registry]\ntoken = "cio_stored"\n')
    runner = FakeRunner()

    assert cargo.logout(runner) is True
    assert runner.ran("cargo logout")


def test_a_failed_logout_is_reported(cargo_home):
    store(cargo_home, '[registry]\ntoken = "cio_stored"\n')
    runner = FakeRunner().reply("cargo logout", returncode=101, stderr="locked")

    with pytest.raises(VommitError, match="`cargo logout` failed: locked"):
        cargo.logout(runner)


def test_token_instructions_name_where_to_get_one_and_where_it_goes(cargo_home):
    instructions = cargo.token_instructions()

    assert "https://crates.io/settings/tokens" in instructions
    assert str(cargo_home / "credentials.toml") in instructions
    assert cargo.TOKEN_VAR in instructions
