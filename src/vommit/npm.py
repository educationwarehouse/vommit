"""
Releasing a project whose version lives in `package.json`.

Everything specific to npm and bun: which of the two to run, the commands
`setup` suggests, and the `.npmrc` credential a publish authenticates with.
The version arithmetic itself is `versioning.NpmProject`.
"""

import json
import shutil
import typing as t
from pathlib import Path

from .config import CommandConfig
from .errors import VommitError
from .shell import Runner
from .versioning import PACKAGE_JSON

Binary = t.Literal["bun", "npm"]

BUILD_COMMANDS: dict[Binary, str] = {"bun": "bun run build", "npm": "npm run build"}
PUBLISH_COMMANDS: dict[Binary, str] = {"bun": "bun publish", "npm": "npm publish"}
WHOAMI_COMMANDS: dict[Binary, str] = {"bun": "bun pm whoami", "npm": "npm whoami"}

REGISTRY = "registry.npmjs.org"

# where npm and bun both read a publish credential. A project-local `.npmrc`
# works too, but is likelier to be committed with a live token in it.
NPMRC = Path.home() / ".npmrc"

TOKEN_ADVICE = (
    "`npm publish`/`bun publish` will ask for a live one-time password on "
    "every release if the package requires 2FA to publish, which is the "
    "default. An Automation Token, or a Granular Access Token with "
    '"Bypass two-factor authentication" enabled, is exempt from that. '
    "Generate one at https://www.npmjs.com/settings/~/tokens (checking that "
    "box explicitly). A token from `npm login`/`bunx npm login` does NOT "
    "count here: it does not carry that exemption, so it will still be asked "
    "for an OTP on every publish."
)


def auth_token_setting(registry: str = REGISTRY) -> str:
    """
    The config key holding a publish credential for `registry`.

    Used by the `.npmrc` line, the `npm_config_` environment override and the
    instructions for writing either, so it is spelled once here.
    """
    return f"//{registry}/:_authToken"


def binary() -> Binary:
    """
    Which JS package manager to run commands with.

    bun first: installing npm means installing Node, hundreds of megabytes a
    project may have no other use for.
    """
    if shutil.which("bun"):
        return "bun"
    elif shutil.which("npm"):
        return "npm"
    else:
        raise VommitError(
            "Neither `npm` nor `bun` is on PATH. Install one of them, set "
            "`commands.publish` (and `commands.build`, if needed) yourself, "
            "or authenticate later once one is installed."
        )


def whoami_command() -> str:
    """
    The read-only command confirming a credential authenticates at all.

    It cannot confirm the token also carries the 2FA exemption a publish
    needs; see `TOKEN_ADVICE`.
    """
    return WHOAMI_COMMANDS[binary()]


def suggested_commands(root: Path) -> CommandConfig | None:
    """
    The release commands `setup` offers for an npm project.

    Whether this *is* one is the caller's question. `build` is suggested only
    when `package.json` declares a "build" script: plenty of packages have no
    build step, and one that fails every release is worse than none.

    Raises `VommitError` when neither `npm` nor `bun` is on PATH.
    """
    if not (root / PACKAGE_JSON).exists():  # pragma: no cover - backstop, not a path
        return None

    manager = binary()
    commands = CommandConfig()
    commands.build = BUILD_COMMANDS[manager] if _has_build_script(root) else ""
    commands.publish = PUBLISH_COMMANDS[manager]
    return commands


def has_auth_token(npmrc: Path = NPMRC, registry: str = REGISTRY) -> bool:
    """
    Whether `npmrc` configures a publish credential for `registry`.

    Only the `_authToken` form, which is what `setup` writes. A project
    authenticating some other way gets asked again. That is a nag, not a bug:
    none of this is ever a precondition for publishing to succeed.
    """
    if not npmrc.exists():
        return False
    return any(
        line.strip().startswith(_token_line_prefix(registry))
        for line in npmrc.read_text().splitlines()
    )


def write_auth_token(token: str, npmrc: Path = NPMRC, registry: str = REGISTRY) -> None:
    """
    Stores `token` in `npmrc`, replacing `registry`'s line instead of
    appending a second, conflicting one on every re-authentication.
    """
    prefix = _token_line_prefix(registry)
    line = f"{prefix}{token}\n"
    if not npmrc.exists():
        # 0600 before the token goes in, so it is never readable at whatever
        # the umask allows. An existing file keeps its mode.
        npmrc.touch(mode=0o600)
        npmrc.write_text(line)
        return

    lines = npmrc.read_text().splitlines(keepends=True)
    for index, existing in enumerate(lines):
        if existing.strip().startswith(prefix):
            lines[index] = line
            npmrc.write_text("".join(lines))
            return

    text = "".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    npmrc.write_text(text + line)


def clear_auth_token(npmrc: Path = NPMRC, registry: str = REGISTRY) -> bool:
    """
    Removes `registry`'s `_authToken` line, reporting whether there was one,
    as `TokenStore.forget` does for the PyPI keyring.
    """
    if not npmrc.exists():
        return False
    prefix = _token_line_prefix(registry)
    lines = npmrc.read_text().splitlines(keepends=True)
    kept = [line for line in lines if not line.strip().startswith(prefix)]
    if len(kept) == len(lines):
        return False
    npmrc.write_text("".join(kept))
    return True


def token_env(token: str, registry: str = REGISTRY) -> dict[str, str]:
    """
    The environment that authenticates npm or bun as `token`, without either
    reading `npmrc` for it.

    Both spellings: npm reads only the registry-scoped key and bun only
    `NPM_CONFIG_TOKEN` (measured against npm 10.9.7 and bun 1.2). Set one and
    npm falls back to `npmrc`, so a mistyped token verifies against the
    credential already on disk and then overwrites it.
    """
    return {
        "NPM_CONFIG_TOKEN": token,
        f"npm_config_{auth_token_setting(registry)}": token,
    }


def verify_token(token: str, runner: Runner) -> None:
    """
    Confirms `token` authenticates before it is written anywhere, so a
    rejected one leaves `npmrc` as it was.
    """
    command = whoami_command()
    result = runner.run(command, env=token_env(token))
    if not result.ok:
        raise VommitError(
            f"`{command}` failed: {result.error}\nThis token does not seem "
            "to work; check it and try again."
        )


def token_instructions(npmrc: Path = NPMRC, registry: str = REGISTRY) -> str:
    """
    `TOKEN_ADVICE` plus where the token goes, for `setup`, which suggests the
    line without writing it.
    """
    return (
        f"No npm publish credential found. {TOKEN_ADVICE}\nAdd\n"
        f"    {_token_line_prefix(registry)}<token>\n"
        f"to {npmrc}."
    )


def _token_line_prefix(registry: str) -> str:
    return f"{auth_token_setting(registry)}="


def _has_build_script(root: Path) -> bool:
    package_json = root / PACKAGE_JSON
    if not package_json.exists():  # pragma: no cover - backstop, not a path
        return False
    try:
        data = json.loads(package_json.read_text())
    except json.JSONDecodeError:  # pragma: no cover - backstop, not a path
        return False
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return isinstance(scripts, dict) and bool(str(scripts.get("build", "")).strip())
