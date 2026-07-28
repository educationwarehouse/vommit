"""
The PyPI token: where it comes from, and whether it looks usable.

vommit resolves the token itself rather than leaving it to the publish command,
so that any publish tool works the same way. The token therefore passes through
this process, which is why it only ever travels as a return value or an `env`
mapping: never onto a `CommandResult`, never into a message.

Three places are tried, in this order: the environment (so CI needs nothing set
up), the keyring, and finally the person at the keyboard.

Both things this module talks to are held rather than imported at the point of
use: `TokenStore` takes its keyring the way `GitRepo` takes its runner, and the
index probe takes the function that posts. A test hands over a stand-in; nothing
has to reach into this module and swap its globals out.
"""

import dataclasses as dc
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

# where a token was found, for saying so out loud
ENVIRONMENT = f"{TOKEN_VAR} environment variable"
KEYRING = "keyring"
PROMPT = "prompt"

UPLOAD_URL = "https://upload.pypi.org/legacy/"
# enough of an upload for the index to authenticate it before finding it wanting
UPLOAD_FORM = {":action": "file_upload", "protocol_version": "1"}
# A valid token gets through authentication and then is refused for the
# deliberately incomplete upload form. Any other response is inconclusive.
ACCEPTED = 400

Authenticate = t.Callable[[], str]
Notify = t.Callable[[str], None]

_NO_BACKEND = (
    "Over SSH or on a headless machine, install `vommit[ssh]` for a backend "
    "that works through ssh-agent. Otherwise set `pypi.use_keyring = false` to "
    f"be asked for the token instead, or set {TOKEN_VAR} in the environment."
)


class Keyring(t.Protocol):
    """
    The three calls this module makes into a keyring.

    The `keyring` module itself satisfies this, and so does a dictionary with
    three methods around it, which is what the tests use.

    Positional-only, because the real module spells the first parameter
    `service_name` and a stand-in has no reason to copy that.
    """

    def get_password(
        self, service: str, username: str, /
    ) -> str | None: ...  # pragma: no cover

    def set_password(
        self, service: str, username: str, password: str, /
    ) -> None: ...  # pragma: no cover

    def delete_password(
        self, service: str, username: str, /
    ) -> None: ...  # pragma: no cover


class Response(t.Protocol):
    """
    As much of an HTTP response as the probe reads.
    """

    status_code: int


#: Posts the probe upload. `requests.post` is the one that really does.
Poster = t.Callable[..., Response]


def environment_token() -> str | None:
    """
    A token the environment already provides; the path CI takes.
    """
    return os.environ.get(TOKEN_VAR) or None


@dc.dataclass(frozen=True)
class TokenStore:
    """
    The PyPI token as this machine keeps it.

    Every keyring failure becomes a `VommitError` naming a way out, because the
    common one is not a bug but a headless machine with no backend installed.
    """

    backend: Keyring = keyring
    service: str = SERVICE
    username: str = USERNAME

    def stored(self) -> str | None:
        """
        The token in the keyring, or None when nothing is stored there yet.
        """
        try:
            return self.backend.get_password(self.service, self.username) or None
        except keyring.errors.KeyringError as error:
            raise VommitError(
                f"Could not read the keyring: {error}\n{_NO_BACKEND}"
            ) from error

    def store(self, token: str) -> None:
        """
        Write (or overwrite) the token, so rotating one is a matter of storing it.
        """
        try:
            self.backend.set_password(self.service, self.username, token.strip())
        except keyring.errors.KeyringError as error:
            raise VommitError(
                f"Could not write to the keyring: {error}\n{_NO_BACKEND}"
            ) from error

    def forget(self) -> bool:
        """
        Remove the stored token; False when there was nothing to remove.
        """
        try:
            self.backend.delete_password(self.service, self.username)
        except keyring.errors.PasswordDeleteError:
            return False
        except keyring.errors.KeyringError as error:
            raise VommitError(
                f"Could not clear the keyring: {error}\n{_NO_BACKEND}"
            ) from error
        return True

    def available(self) -> tuple[str, str] | None:
        """
        The token a release would pick up, and where it came from.
        """
        if token := environment_token():
            return ENVIRONMENT, token
        if token := self.stored():
            return KEYRING, token
        return None

    def require(self) -> str:
        """
        A token from the environment or the keyring, or a refusal naming the fix.

        The half of the resolution that can run unattended; entrypoints that can
        prompt wrap this with one that asks.
        """
        if found := self.available():
            return found[1]
        raise VommitError(
            "No PyPI token found; run `vommit authenticate` to store one, "
            f"or set {TOKEN_VAR} in the environment."
        )


def require_token() -> str:
    """
    The default `Authenticate`: this machine's keyring, asking nothing.
    """
    return TokenStore().require()


def mask(token: str) -> str:
    """
    Enough of a token to recognise it by, never enough to use it.

    A short value is not a real token, so showing nine characters of it would
    give away most of whatever it is; those get the tail only.
    """
    tail = token[-4:]
    return f"{token[:9]}...{tail}" if len(token) >= 20 else f"...{tail}"


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


def probe(
    token: str,
    url: str = UPLOAD_URL,
    timeout: float = 10.0,
    post: Poster = requests.post,
) -> int | None:
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
        response = post(
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
    post: Poster = requests.post,
) -> str:
    """
    The token, checked as far as it can be checked before a real upload.

    An index that cannot be reached is not held against the token: refusing to
    store one because the network is down would be its own kind of wrong.
    """
    token = check_format(token)
    status = probe(token, url, timeout, post)
    if status is None:
        notify("Could not reach PyPI to check the token; taking it as given.")
    elif status != ACCEPTED:
        raise VommitError(
            f"PyPI could not verify this token ({status}). Create a new one at "
            "https://pypi.org/manage/account/token/ and try again."
        )
    return token


def publish_env(token: str | None) -> dict[str, str]:
    """
    The environment a publish command needs, empty when there is no token.
    """
    return {TOKEN_VAR: token} if token else {}
