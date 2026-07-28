"""
The PyPI token, and where it is kept.

vommit reads the token itself rather than leaving it to the publish command,
so that any publish tool works the same way. The token therefore passes through
this process, which is why it only ever travels as a return value or an `env`
mapping: never onto a `CommandResult`, never into a message.
"""

import typing as t

import keyring

from .errors import VommitError

SERVICE: t.Final = "vommit"
USERNAME: t.Final = "pypi"

TOKEN_VAR: t.Final = "UV_PUBLISH_TOKEN"

Authenticate = t.Callable[[], str]


def stored_token(service: str = SERVICE, username: str = USERNAME) -> str | None:
    """
    The token in the keyring, or None when nothing is stored there yet.
    """
    return keyring.get_password(service, username) or None


def store_token(
    token: str,
    service: str = SERVICE,
    username: str = USERNAME,
) -> None:
    """
    Write (or overwrite) the token, so rotating one is a matter of storing it.
    """
    if not token.strip():
        raise VommitError("An empty PyPI token cannot be stored.")
    keyring.set_password(service, username, token.strip())


def require_token(service: str = SERVICE, username: str = USERNAME) -> str:
    """
    The stored token, or a refusal naming the command that stores one.

    This is the non-interactive half of `ensure_authenticated`: entrypoints that
    can prompt pass their own callable instead.
    """
    token = stored_token(service, username)
    if not token:
        raise VommitError(
            "No PyPI token stored; run `vommit authenticate` to add one, "
            "or set pypi.use_keyring = false to use the environment instead."
        )
    return token


def publish_env(token: str | None) -> dict[str, str]:
    """
    The environment a publish command needs, empty when there is no token.
    """
    return {TOKEN_VAR: token} if token else {}
