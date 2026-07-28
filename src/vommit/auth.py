"""
The PyPI token: where it comes from, and whether it looks usable.

vommit resolves the token itself rather than leaving it to the publish command,
so that any publish tool works the same way. The token therefore passes through
this process, which is why it only ever travels as a return value or an `env`
mapping: never onto a `CommandResult`, never into a message.

Three places are tried, in this order: the environment (so CI needs nothing set
up), the keyring, and finally the person at the keyboard.
"""

import os
import typing as t

import keyring
import keyring.errors
import requests

from .errors import VommitError

SERVICE = "vommit"
USERNAME = "pypi"

TOKEN_VAR = "UV_PUBLISH_TOKEN"
TOKEN_PREFIX = "pypi-"

UPLOAD_URL = "https://upload.pypi.org/legacy/"
# enough of an upload for the index to authenticate it before finding it wanting
UPLOAD_FORM = {":action": "file_upload", "protocol_version": "1"}
# only the credentials themselves are answered with a 401 or a 403; anything
# else means the request got past authentication and failed on its own merits
REJECTED = frozenset({401, 403})

Authenticate = t.Callable[[], str]
Notify = t.Callable[[str], None]

_NO_BACKEND = (
    "Over SSH or on a headless machine, install `vommit[ssh]` for a backend "
    "that works through ssh-agent. Otherwise set `pypi.use_keyring = false` to "
    f"be asked for the token instead, or set {TOKEN_VAR} in the environment."
)


def environment_token() -> str | None:
    """
    A token the environment already provides; the path CI takes.
    """
    return os.environ.get(TOKEN_VAR) or None


def stored_token(service: str = SERVICE, username: str = USERNAME) -> str | None:
    """
    The token in the keyring, or None when nothing is stored there yet.
    """
    try:
        return keyring.get_password(service, username) or None
    except keyring.errors.KeyringError as error:
        raise VommitError(f"Could not read the keyring: {error}\n{_NO_BACKEND}")


def store_token(
    token: str,
    service: str = SERVICE,
    username: str = USERNAME,
) -> None:
    """
    Write (or overwrite) the token, so rotating one is a matter of storing it.
    """
    try:
        keyring.set_password(service, username, token.strip())
    except keyring.errors.KeyringError as error:
        raise VommitError(f"Could not write to the keyring: {error}\n{_NO_BACKEND}")


def require_token(service: str = SERVICE, username: str = USERNAME) -> str:
    """
    A token from the environment or the keyring, or a refusal naming the fix.

    The half of the resolution that can run unattended; entrypoints that can
    prompt wrap this with one that asks.
    """
    token = environment_token() or stored_token(service, username)
    if not token:
        raise VommitError(
            "No PyPI token found; run `vommit authenticate` to store one, "
            f"or set {TOKEN_VAR} in the environment."
        )
    return token


def check_format(token: str) -> str:
    """
    The token, stripped, if it looks like one at all.

    Catches the realistic mistake, which is a half-copied clipboard rather than
    a revoked token: only the index can tell you about the latter.
    """
    token = token.strip()
    if not token:
        raise VommitError("An empty PyPI token cannot be stored.")
    if not token.startswith(TOKEN_PREFIX):
        raise VommitError(
            f"A PyPI token starts with {TOKEN_PREFIX!r}, and this one does not. "
            "Check what you pasted, or pass --no-verify if you publish to an "
            "index that issues a different kind of token."
        )
    return token


def probe(token: str, url: str = UPLOAD_URL, timeout: float = 10.0) -> int | None:
    """
    What the index answers to these credentials, or None if it was unreachable.

    The upload announces itself and then says nothing else, which is deliberate:
    a token that works gets as far as being told the request is incomplete, one
    that does not is turned away at the door with a 403.

    The body has to be there. A POST to `/legacy/` with an empty body is
    answered 405 before the credentials are looked at, which made an earlier
    version of this check accept everything.
    """
    try:
        response = requests.post(
            url,
            auth=("__token__", token),
            data=UPLOAD_FORM,
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    return response.status_code


def verify_token(
    token: str,
    url: str = UPLOAD_URL,
    timeout: float = 10.0,
    notify: Notify = lambda _: None,
) -> str:
    """
    The token, checked as far as it can be checked before a real upload.

    An index that cannot be reached is not held against the token: refusing to
    store one because the network is down would be its own kind of wrong.
    """
    token = check_format(token)
    status = probe(token, url, timeout)
    if status is None:
        notify("Could not reach PyPI to check the token; taking it as given.")
    elif status in REJECTED:
        raise VommitError(
            f"PyPI rejected this token ({status}). Create a new one at "
            "https://pypi.org/manage/account/token/ and try again."
        )
    return token


def publish_env(token: str | None) -> dict[str, str]:
    """
    The environment a publish command needs, empty when there is no token.
    """
    return {TOKEN_VAR: token} if token else {}
