import pytest

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


@pytest.fixture
def fake_keyring(monkeypatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(auth.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(auth.keyring, "set_password", fake.set_password)
    return fake


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


def test_store_token_refuses_an_empty_token(fake_keyring):
    with pytest.raises(VommitError, match="empty PyPI token"):
        auth.store_token("   ")


def test_require_token_returns_the_stored_one(fake_keyring):
    fake_keyring.stored[("vommit", "pypi")] = "pypi-abc"

    assert auth.require_token() == "pypi-abc"


def test_require_token_names_the_command_that_fixes_it(fake_keyring):
    with pytest.raises(VommitError, match="vommit authenticate"):
        auth.require_token()


def test_publish_env():
    assert auth.publish_env("pypi-abc") == {"UV_PUBLISH_TOKEN": "pypi-abc"}
    assert auth.publish_env(None) == {}
