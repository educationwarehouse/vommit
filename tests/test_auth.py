import keyring.errors
import pytest

import requests

from src.vommit import auth
from src.vommit.errors import VommitError


class FakeKeyring:
    """
    Stands in for the keyring backend; the real one wants a session daemon.
    """

    def __init__(self, stored: dict[tuple[str, str], str] | None = None) -> None:
        self.stored = stored or {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.stored.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.stored[(service, username)] = password


class BrokenKeyring:
    """
    A machine with no keyring backend, which is the default on headless Linux.
    """

    def get_password(self, service: str, username: str) -> str | None:
        raise keyring.errors.NoKeyringError("No recommended backend was available")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise keyring.errors.NoKeyringError("No recommended backend was available")


@pytest.fixture
def fake_keyring(monkeypatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(auth.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(auth.keyring, "set_password", fake.set_password)
    monkeypatch.delenv(auth.TOKEN_VAR, raising=False)
    return fake


@pytest.fixture
def broken_keyring(monkeypatch) -> None:
    broken = BrokenKeyring()
    monkeypatch.setattr(auth.keyring, "get_password", broken.get_password)
    monkeypatch.setattr(auth.keyring, "set_password", broken.set_password)


@pytest.fixture
def no_env_token(monkeypatch) -> None:
    monkeypatch.delenv(auth.TOKEN_VAR, raising=False)


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def answering(status: int | None, seen: dict | None = None):
    """
    Stands in for requests.post, so no test needs the network.
    """

    def post(url, auth=None, data=None, timeout=None):
        if seen is not None:
            seen.update(url=url, auth=auth, data=data, timeout=timeout)
        if status is None:
            raise requests.ConnectionError("unreachable")
        return FakeResponse(status)

    return post


def test_stored_token_reads_vommits_own_namespace(fake_keyring):
    fake_keyring.stored[("vommit", "pypi")] = "pypi-abc"

    assert auth.stored_token() == "pypi-abc"


def test_stored_token_is_none_when_nothing_is_stored(fake_keyring):
    assert auth.stored_token() is None


def test_an_empty_entry_counts_as_no_token(fake_keyring):
    # some backends answer "" rather than None for a key they do not have
    fake_keyring.stored[("vommit", "pypi")] = ""

    assert auth.stored_token() is None


def test_store_token_overwrites(fake_keyring):
    auth.store_token("first")
    auth.store_token("second")

    assert auth.stored_token() == "second"


def test_store_token_strips_surrounding_whitespace(fake_keyring):
    auth.store_token("  pypi-abc\n")

    assert auth.stored_token() == "pypi-abc"


def test_require_token_returns_the_stored_one(fake_keyring, no_env_token):
    fake_keyring.stored[("vommit", "pypi")] = "pypi-abc"

    assert auth.require_token() == "pypi-abc"


def test_require_token_prefers_the_environment(fake_keyring, monkeypatch):
    # the CI path: nothing stored, nothing to ask, but the variable is set
    fake_keyring.stored[("vommit", "pypi")] = "pypi-stored"
    monkeypatch.setenv(auth.TOKEN_VAR, "pypi-from-env")

    assert auth.require_token() == "pypi-from-env"


def test_require_token_names_both_ways_out(fake_keyring, no_env_token):
    with pytest.raises(VommitError, match="vommit authenticate"):
        auth.require_token()

    with pytest.raises(VommitError, match=auth.TOKEN_VAR):
        auth.require_token()


def test_environment_token_ignores_an_empty_variable(monkeypatch):
    monkeypatch.setenv(auth.TOKEN_VAR, "")
    assert auth.environment_token() is None


def test_publish_env():
    assert auth.publish_env("pypi-abc") == {"UV_PUBLISH_TOKEN": "pypi-abc"}
    assert auth.publish_env(None) == {}


# --- a machine without a keyring backend --------------------------------------


def test_a_missing_backend_reads_as_a_vommit_error(broken_keyring):
    # regression: keyring's own exception escaped as a traceback
    with pytest.raises(VommitError, match="Could not read the keyring"):
        auth.stored_token()


def test_a_missing_backend_says_how_to_work_around_it(broken_keyring):
    with pytest.raises(VommitError, match="use_keyring = false"):
        auth.stored_token()


def test_a_missing_backend_on_write_reads_as_a_vommit_error(broken_keyring):
    with pytest.raises(VommitError, match="Could not write to the keyring"):
        auth.store_token("pypi-abc")


# --- format ------------------------------------------------------------------


def test_check_format_strips_and_accepts_a_real_token():
    assert auth.check_format("  pypi-abc\n") == "pypi-abc"


def test_check_format_refuses_an_empty_token():
    with pytest.raises(VommitError, match="empty PyPI token"):
        auth.check_format("   ")


def test_check_format_refuses_something_that_is_not_a_token():
    # the realistic mistake: half a clipboard, or the wrong entry entirely
    with pytest.raises(VommitError, match="--no-verify"):
        auth.check_format("hunter2")


# --- the live probe -----------------------------------------------------------


def test_a_rejected_token_is_reported(monkeypatch):
    monkeypatch.setattr(auth.requests, "post", answering(403))

    with pytest.raises(VommitError, match="PyPI rejected this token"):
        auth.verify_token("pypi-abc")


def test_an_unauthorised_answer_counts_as_rejection(monkeypatch):
    monkeypatch.setattr(auth.requests, "post", answering(401))

    with pytest.raises(VommitError, match="401"):
        auth.verify_token("pypi-abc")


def test_a_400_means_the_credentials_were_fine(monkeypatch):
    # an empty upload is malformed on purpose; getting told so is the pass
    monkeypatch.setattr(auth.requests, "post", answering(400))

    assert auth.verify_token("pypi-abc") == "pypi-abc"


def test_an_unreachable_index_is_not_held_against_the_token(monkeypatch):
    monkeypatch.setattr(auth.requests, "post", answering(None))
    said: list[str] = []

    assert auth.verify_token("pypi-abc", notify=said.append) == "pypi-abc"
    assert "Could not reach PyPI" in said[0]


def test_the_format_is_checked_before_the_network(monkeypatch):
    def explode(*_, **__):  # pragma: no cover
        raise AssertionError("a malformed token is not worth a request")

    monkeypatch.setattr(auth.requests, "post", explode)

    with pytest.raises(VommitError, match="starts with"):
        auth.verify_token("hunter2")


def test_an_index_that_answers_success_is_accepted(monkeypatch):
    # not what upload.pypi.org does for an empty body, but a private index might
    monkeypatch.setattr(auth.requests, "post", answering(200))

    assert auth.verify_token("pypi-abc") == "pypi-abc"


def test_the_probe_sends_a_body_and_token_auth(monkeypatch):
    # regression: an empty POST to /legacy/ is answered 405 before the
    # credentials are looked at, so every token came back accepted
    seen: dict = {}
    monkeypatch.setattr(auth.requests, "post", answering(400, seen))

    auth.verify_token("pypi-abc")

    assert seen["auth"] == ("__token__", "pypi-abc")
    assert seen["data"] == auth.UPLOAD_FORM
    assert seen["data"], "an empty body never reaches authentication"


def test_a_405_is_not_taken_as_approval(monkeypatch):
    # what the broken version got every time; it must not read as "fine"
    monkeypatch.setattr(auth.requests, "post", answering(405))
    said: list[str] = []

    auth.verify_token("pypi-abc", notify=said.append)

    assert said == [], "405 is not a rejection, but it is not a blessing either"
