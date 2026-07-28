import pytest

from src.vommit import auth
from src.vommit.auth import TokenStore
from src.vommit.errors import VommitError

from .conftest import BrokenKeyring, FakeIndex, FakeKeyring

TOKEN = "pypi-abc"


@pytest.fixture
def store() -> TokenStore:
    """
    A token store over a dictionary, ready to be read from and written to.
    """
    return TokenStore(backend=FakeKeyring())


@pytest.fixture
def broken() -> TokenStore:
    return TokenStore(backend=BrokenKeyring())


def test_stored_token_reads_vommits_own_namespace(store):
    store.backend.stored[("vommit", "pypi")] = TOKEN

    assert store.stored() == TOKEN


def test_stored_token_is_none_when_nothing_is_stored(store):
    assert store.stored() is None


def test_an_empty_entry_counts_as_no_token(store):
    # some backends answer "" rather than None for a key they do not have
    store.backend.stored[("vommit", "pypi")] = ""

    assert store.stored() is None


def test_a_store_only_sees_its_own_backend():
    # what patching the module could not express: two machines at once
    mine = TokenStore(backend=FakeKeyring())
    theirs = TokenStore(backend=FakeKeyring())
    mine.store(TOKEN)

    assert theirs.stored() is None


def test_the_namespace_is_configurable(store):
    elsewhere = TokenStore(backend=store.backend, service="other", username="someone")
    elsewhere.store(TOKEN)

    assert store.stored() is None
    assert elsewhere.stored() == TOKEN


def test_store_token_overwrites(store):
    store.store("first")
    store.store("second")

    assert store.stored() == "second"


def test_store_token_strips_surrounding_whitespace(store):
    store.store(f"  {TOKEN}\n")

    assert store.stored() == TOKEN


def test_require_token_returns_the_stored_one(store):
    store.store(TOKEN)

    assert store.require() == TOKEN


def test_require_token_prefers_the_environment(store, monkeypatch):
    # the CI path: nothing stored, nothing to ask, but the variable is set
    store.store("pypi-stored")
    monkeypatch.setenv(auth.TOKEN_VAR, "pypi-from-env")

    assert store.require() == "pypi-from-env"


def test_require_token_names_both_ways_out(store):
    with pytest.raises(VommitError, match="vommit authenticate"):
        store.require()

    with pytest.raises(VommitError, match=auth.TOKEN_VAR):
        store.require()


def test_require_token_helper_uses_the_real_keyring(monkeypatch):
    # the default `Authenticate` release() falls back on; the environment is
    # enough to answer it without a backend being installed
    monkeypatch.setenv(auth.TOKEN_VAR, TOKEN)

    assert auth.require_token() == TOKEN


def test_environment_token_ignores_an_empty_variable(monkeypatch):
    monkeypatch.setenv(auth.TOKEN_VAR, "")
    assert auth.environment_token() is None


def test_publish_env():
    assert auth.publish_env(TOKEN) == {"UV_PUBLISH_TOKEN": TOKEN}
    assert auth.publish_env(None) == {}


# --- a machine without a keyring backend --------------------------------------


def test_a_missing_backend_reads_as_a_vommit_error(broken):
    # regression: keyring's own exception escaped as a traceback
    with pytest.raises(VommitError, match="Could not read the keyring"):
        broken.stored()


def test_a_missing_backend_says_how_to_work_around_it(broken):
    with pytest.raises(VommitError, match="use_keyring = false"):
        broken.stored()


def test_a_missing_backend_on_write_reads_as_a_vommit_error(broken):
    with pytest.raises(VommitError, match="Could not write to the keyring"):
        broken.store(TOKEN)


# --- format ------------------------------------------------------------------


def test_check_format_strips_and_accepts_a_real_token():
    assert auth.check_format(f"  {TOKEN}\n") == TOKEN


def test_check_format_refuses_an_empty_token():
    with pytest.raises(VommitError, match="empty PyPI token"):
        auth.check_format("   ")


def test_check_format_refuses_something_that_is_not_a_token():
    # the realistic mistake: half a clipboard, or the wrong entry entirely
    with pytest.raises(VommitError, match="--no-verify"):
        auth.check_format("hunter2")


# --- the live probe -----------------------------------------------------------


def test_a_rejected_token_is_reported():
    with pytest.raises(VommitError, match="PyPI rejected this token"):
        auth.verify_token(TOKEN, post=FakeIndex(403).post)


def test_an_unauthorised_answer_counts_as_rejection():
    with pytest.raises(VommitError, match="401"):
        auth.verify_token(TOKEN, post=FakeIndex(401).post)


def test_a_400_means_the_credentials_were_fine():
    # an empty upload is malformed on purpose; getting told so is the pass
    assert auth.verify_token(TOKEN, post=FakeIndex(400).post) == TOKEN


def test_an_unreachable_index_is_not_held_against_the_token():
    said: list[str] = []

    token = auth.verify_token(TOKEN, notify=said.append, post=FakeIndex(None).post)

    assert token == TOKEN
    assert "Could not reach PyPI" in said[0]


def test_the_format_is_checked_before_the_network():
    def explode(*_, **__):  # pragma: no cover
        raise AssertionError("a malformed token is not worth a request")

    with pytest.raises(VommitError, match="starts with"):
        auth.verify_token("hunter2", post=explode)


def test_an_index_that_answers_success_is_accepted():
    # not what upload.pypi.org does for an empty body, but a private index might
    assert auth.verify_token(TOKEN, post=FakeIndex(200).post) == TOKEN


def test_the_probe_sends_a_body_and_token_auth():
    # regression: an empty POST to /legacy/ is answered 405 before the
    # credentials are looked at, so every token came back accepted
    index = FakeIndex(400)

    auth.verify_token(TOKEN, post=index.post)

    assert index.seen["auth"] == ("__token__", TOKEN)
    assert index.seen["data"] == auth.UPLOAD_FORM
    assert index.seen["data"], "an empty body never reaches authentication"


def test_the_probe_talks_to_the_upload_endpoint():
    index = FakeIndex(400)

    auth.verify_token(TOKEN, post=index.post)

    assert index.seen["url"] == auth.UPLOAD_URL
    assert index.seen["timeout"] == 10.0


def test_a_405_is_not_taken_as_approval():
    # what the broken version got every time; it must not read as "fine"
    said: list[str] = []

    auth.verify_token(TOKEN, notify=said.append, post=FakeIndex(405).post)

    assert said == [], "405 is not a rejection, but it is not a blessing either"


# --- forgetting and showing ---------------------------------------------------


def test_forget_token_removes_it(store):
    store.store(TOKEN)

    assert store.forget() is True
    assert store.stored() is None


def test_forget_token_is_false_when_there_was_nothing(store):
    assert store.forget() is False


def test_forget_token_reports_a_broken_backend(broken):
    with pytest.raises(VommitError, match="Could not clear the keyring"):
        broken.forget()


def test_mask_shows_the_ends_of_a_real_token():
    token = "pypi-AgEIcHlwaS5vcmcCJDU0NzhhMjRlLWQxMmMtNDkxNC1iN2Y0LWFi"

    assert auth.mask(token) == "pypi-AgEI...LWFi"
    assert token[10:-4] not in auth.mask(token)


def test_mask_of_something_short_gives_away_only_the_tail():
    # not a real token, so nine characters would be most of it
    assert auth.mask("pypi-123") == "...-123"


def test_available_token_prefers_the_environment(store, monkeypatch):
    store.store("pypi-stored")
    monkeypatch.setenv(auth.TOKEN_VAR, "pypi-env")

    assert store.available() == (auth.ENVIRONMENT, "pypi-env")


def test_available_token_falls_back_to_the_keyring(store):
    store.store("pypi-stored")

    assert store.available() == (auth.KEYRING, "pypi-stored")


def test_available_token_is_none_when_there_is_nothing(store):
    assert store.available() is None
