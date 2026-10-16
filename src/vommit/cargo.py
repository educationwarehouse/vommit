"""
Releasing a Rust crate to crates.io.

Everything specific to a crate published by itself: the commands `setup`
suggests and the credential `cargo publish` authenticates with. The version
arithmetic itself is `versioning.CrateProject`.
"""

import os
import shlex
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError

from .config import CommandConfig
from .errors import VommitError
from .shell import Runner, shell

# `cargo package` builds the `.crate` and verify-compiles it, so a crate that
# would fail to publish stops the release before anything is pushed. Not
# `cargo build --release`: publishing never uploads a binary, and its own
# verification builds the dev profile, so a release build is time spent on
# nothing. `cargo publish` verifies a second time, mostly from that cache.
BUILD_COMMAND = "cargo package"
PUBLISH_COMMAND = "cargo publish"

TOKEN_VAR = "CARGO_REGISTRY_TOKEN"
# carries a token into `cargo login`'s stdin; not TOKEN_VAR, which cargo would
# read as the credential itself
LOGIN_VAR = "VOMMIT_CARGO_TOKEN"

# `cargo login` hands the token to whichever credential provider is configured;
# the default one keeps it here
CREDENTIAL_FILES = ("credentials.toml", "credentials")

TOKEN_ADVICE = (
    "Generate an API token with the publish-update scope (and publish-new, "
    "for a crate that has never been published) at "
    "https://crates.io/settings/tokens."
)


def cargo_home() -> Path:
    return Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo")


def suggested_commands(workspace: bool = False) -> CommandConfig:
    """
    The release commands `setup` offers for a crate, or for every crate in a
    workspace.

    `--workspace` for a workspace: a virtual manifest has no package of its own
    for a bare `cargo publish` to publish, and members sharing the version are
    released together. Cargo publishes them in dependency order and skips any
    marked `publish = false`.

    `clean` is empty: `cargo clean` throws away the whole `target/` directory,
    and with it the cache that keeps the double verification cheap.
    """
    suffix = " --workspace" if workspace else ""
    commands = CommandConfig()
    commands.clean = ""
    commands.build = BUILD_COMMAND + suffix
    commands.publish = PUBLISH_COMMAND + suffix
    return commands


def token_source(home: Path | None = None) -> str | None:
    """
    Where `cargo publish` will find a crates.io credential, None for nowhere.

    The environment first, as cargo itself reads it, then the file the default
    credential provider writes. Anything else (a keychain provider, `--token`
    in the publish command) is asked about again; as with npm that is a nag,
    never a precondition.
    """
    if os.environ.get(TOKEN_VAR):
        return f"${TOKEN_VAR}"
    if stored := stored_token_file(home):
        return str(stored)
    return None


def has_token(home: Path | None = None) -> bool:
    return token_source(home) is not None


def stored_token_file(home: Path | None = None) -> Path | None:
    """
    The default credential provider's file, if it holds a crates.io token.
    """
    home = home or cargo_home()
    # Cargo prefers the legacy file whenever it exists, even without a token.
    path = next(
        (home / name for name in reversed(CREDENTIAL_FILES) if (home / name).exists()),
        None,
    )
    if path is None:
        return None
    try:
        registry = tomlkit.parse(path.read_text()).get("registry")
    except ParseError:
        return None
    if isinstance(registry, dict) and registry.get("token"):
        return path
    return None


def login(token: str, runner: Runner) -> None:
    """
    Stores `token` the way `cargo login` would, because it is `cargo login`.

    Fed on stdin through a variable, never as an argument: an argument shows
    up in the process list for as long as cargo runs.
    """
    command = shell(f'printf %s "${LOGIN_VAR}" | {shlex.join(["cargo", "login"])}')
    result = runner.run(command, env={LOGIN_VAR: token})
    if not result.ok:
        raise VommitError(f"`cargo login` failed: {result.error}")


def logout(runner: Runner) -> bool:
    """
    Removes the stored credential, reporting whether there was one, as
    `TokenStore.forget` does for the PyPI keyring.
    """
    # the stored one only: `cargo logout` cannot unset the environment
    if not stored_token_file():
        return False
    result = runner.run(shlex.join(["cargo", "logout"]))
    if not result.ok:
        raise VommitError(f"`cargo logout` failed: {result.error}")
    return True


def token_instructions(home: Path | None = None) -> str:
    """
    What `setup` and `ensure-authenticated` say when there is no credential.
    """
    return (
        f"No crates.io publish credential found. {TOKEN_ADVICE}\n"
        "Store it with `vommit authenticate` (or `cargo login`), which writes "
        f"it to {(home or cargo_home()) / CREDENTIAL_FILES[0]}, or provide "
        f"{TOKEN_VAR} in the environment."
    )
