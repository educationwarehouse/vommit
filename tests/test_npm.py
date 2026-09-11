"""
npm and bun: which one to run, what `setup` suggests, and the `.npmrc`
credential a publish authenticates with.
"""

import json
import stat
from pathlib import Path

import pytest

from src.vommit import npm
from src.vommit.errors import VommitError

from .conftest import FakeRunner

UV_BUILD_BACKEND = (
    '[build-system]\nrequires = ["uv_build>=0.12.4,<0.13"]\n'
    'build-backend = "uv_build"\n'
)


def project(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(body)
    return root


def npm_package(root: Path, scripts: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    data = {"name": "pkg", "version": "1.0.0"}
    if scripts is not None:
        data["scripts"] = scripts
    (root / "package.json").write_text(json.dumps(data, indent=2) + "\n")
    return root


def only_on_path(monkeypatch, tmp_path: Path, *names: str) -> None:
    """
    Puts fake, real-enough-to-run executables named `names` on an otherwise
    empty PATH, so a test controls exactly what `shutil.which` finds instead
    of depending on what happens to be installed on whatever machine runs it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


def test_suggested_commands_is_none_for_a_python_project(tmp_path, monkeypatch):
    """
    Whether the project is npm-versioned is the caller's question (it has the
    resolved `VersionSource` already); all this checks is that there is a
    manifest to suggest commands for.
    """
    only_on_path(monkeypatch, tmp_path, "npm", "bun")
    root = project(
        tmp_path / "root",
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    )
    assert npm.suggested_commands(root) is None


def test_suggested_commands_is_none_without_a_package_json(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "npm", "bun")
    root = tmp_path / "root"
    root.mkdir()
    assert npm.suggested_commands(root) is None


def test_suggested_commands_prefers_bun_when_both_are_installed(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "npm", "bun")
    root = npm_package(tmp_path / "root")
    suggestion = npm.suggested_commands(root)

    assert suggestion is not None
    assert suggestion.publish == npm.PUBLISH_COMMANDS["bun"]


def test_suggested_commands_falls_back_to_npm_without_bun(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "npm")
    root = npm_package(tmp_path / "root")
    suggestion = npm.suggested_commands(root)

    assert suggestion is not None
    assert suggestion.publish == npm.PUBLISH_COMMANDS["npm"]


def test_suggested_commands_refuses_to_guess_without_npm_or_bun(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path)
    root = npm_package(tmp_path / "root")

    with pytest.raises(VommitError, match="Neither `npm` nor `bun`"):
        npm.suggested_commands(root)


def test_suggested_commands_skips_build_by_default(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "bun")
    root = npm_package(tmp_path / "root")
    suggestion = npm.suggested_commands(root)

    assert suggestion is not None
    assert suggestion.build == ""


def test_suggested_commands_offers_a_build_step_when_the_script_exists(
    tmp_path, monkeypatch
):
    only_on_path(monkeypatch, tmp_path, "bun")
    root = npm_package(tmp_path / "root", scripts={"build": "tsc"})
    suggestion = npm.suggested_commands(root)

    assert suggestion is not None
    assert suggestion.build == npm.BUILD_COMMANDS["bun"]


def test_suggested_commands_offers_the_matching_build_step_without_bun(
    tmp_path, monkeypatch
):
    only_on_path(monkeypatch, tmp_path, "npm")
    root = npm_package(tmp_path / "root", scripts={"build": "tsc"})
    suggestion = npm.suggested_commands(root)

    assert suggestion is not None
    assert suggestion.build == npm.BUILD_COMMANDS["npm"]


def test_suggested_commands_ignores_a_blank_build_script(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "bun")
    root = npm_package(tmp_path / "root", scripts={"build": "   "})
    suggestion = npm.suggested_commands(root)

    assert suggestion is not None
    assert suggestion.build == ""


def test_suggested_commands_leaves_clean_and_post_publish_at_their_defaults(
    tmp_path, monkeypatch
):
    only_on_path(monkeypatch, tmp_path, "bun")
    root = npm_package(tmp_path / "root", scripts={"build": "tsc"})
    suggestion = npm.suggested_commands(root)

    assert suggestion is not None
    assert suggestion.clean == "rm -rf ./dist"
    assert suggestion.post_publish == ""


def test_has_npm_auth_token_is_false_without_an_npmrc(tmp_path):
    assert npm.has_auth_token(tmp_path / ".npmrc") is False


def test_has_npm_auth_token_is_false_for_an_unrelated_npmrc(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("registry=https://registry.npmjs.org/\nsave-exact=true\n")
    assert npm.has_auth_token(npmrc) is False


def test_has_npm_auth_token_is_true_once_a_token_is_configured(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("//registry.npmjs.org/:_authToken=abc123\n")
    assert npm.has_auth_token(npmrc) is True


def test_has_npm_auth_token_is_scoped_to_the_given_registry(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("//registry.example.com/:_authToken=abc123\n")
    assert npm.has_auth_token(npmrc) is False
    assert npm.has_auth_token(npmrc, registry="registry.example.com") is True


def test_has_npm_auth_token_ignores_leading_whitespace(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("  //registry.npmjs.org/:_authToken=abc123\n")
    assert npm.has_auth_token(npmrc) is True


def test_npm_token_instructions_names_the_registry_line_and_the_file(tmp_path):
    npmrc = tmp_path / ".npmrc"
    instructions = npm.token_instructions(npmrc, registry="registry.example.com")
    assert "//registry.example.com/:_authToken=<token>" in instructions
    assert str(npmrc) in instructions


def test_npm_token_instructions_includes_where_to_get_one():
    instructions = npm.token_instructions()
    assert "https://www.npmjs.com/settings/~/tokens" in instructions
    assert "Bypass two-factor authentication" in instructions


def test_npm_token_advice_names_the_url_and_the_setting_to_check():
    advice = npm.TOKEN_ADVICE
    assert "https://www.npmjs.com/settings/~/tokens" in advice
    assert "Bypass two-factor authentication" in advice


def test_npm_token_advice_warns_off_a_plain_login_token():
    advice = npm.TOKEN_ADVICE
    assert "npm login" in advice
    assert "does NOT count" in advice


def test_write_npm_auth_token_creates_a_new_file(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npm.write_auth_token("abc123", npmrc)
    assert npmrc.read_text() == "//registry.npmjs.org/:_authToken=abc123\n"


def test_write_npm_auth_token_creates_the_file_unreadable_to_others(tmp_path):
    """
    The line being written is a live publish credential, so the file must not
    exist at the umask default for even an instant. npm writes it 0600 too.
    """
    npmrc = tmp_path / ".npmrc"
    npm.write_auth_token("abc123", npmrc)

    assert stat.S_IMODE(npmrc.stat().st_mode) == 0o600


def test_write_npm_auth_token_leaves_an_existing_files_mode_alone(tmp_path):
    """
    Only a file this creates gets its mode chosen here: an `npmrc` that already
    exists may have been widened deliberately, and this is adding a line to
    somebody else's file.
    """
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("//other.registry/:_authToken=old\n")
    npmrc.chmod(0o644)

    npm.write_auth_token("abc123", npmrc)

    assert stat.S_IMODE(npmrc.stat().st_mode) == 0o644


def test_write_npm_auth_token_appends_to_an_existing_file(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("registry=https://registry.npmjs.org/\n")
    npm.write_auth_token("abc123", npmrc)
    assert npmrc.read_text() == (
        "registry=https://registry.npmjs.org/\n"
        "//registry.npmjs.org/:_authToken=abc123\n"
    )


def test_write_npm_auth_token_appends_a_missing_trailing_newline_first(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("registry=https://registry.npmjs.org/")
    npm.write_auth_token("abc123", npmrc)
    assert npmrc.read_text() == (
        "registry=https://registry.npmjs.org/\n"
        "//registry.npmjs.org/:_authToken=abc123\n"
    )


def test_write_npm_auth_token_replaces_rather_than_duplicates(tmp_path):
    """
    A plain append would leave two conflicting `_authToken` lines for the
    same registry after a second `vommit authenticate`.
    """
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text(
        "always-auth=true\n"
        "//registry.npmjs.org/:_authToken=old-token\n"
        "save-exact=true\n"
    )
    npm.write_auth_token("new-token", npmrc)
    assert npmrc.read_text() == (
        "always-auth=true\n"
        "//registry.npmjs.org/:_authToken=new-token\n"
        "save-exact=true\n"
    )


def test_write_npm_auth_token_only_replaces_the_matching_registry(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("//registry.example.com/:_authToken=other\n")
    npm.write_auth_token("abc123", npmrc, registry="registry.npmjs.org")
    assert npmrc.read_text() == (
        "//registry.example.com/:_authToken=other\n"
        "//registry.npmjs.org/:_authToken=abc123\n"
    )


def test_clear_npm_auth_token_removes_the_line_and_reports_it(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("always-auth=true\n//registry.npmjs.org/:_authToken=abc123\n")

    assert npm.clear_auth_token(npmrc) is True
    assert npmrc.read_text() == "always-auth=true\n"


def test_clear_npm_auth_token_reports_when_there_was_nothing_to_clear(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("always-auth=true\n")

    assert npm.clear_auth_token(npmrc) is False
    assert npmrc.read_text() == "always-auth=true\n"


def test_clear_npm_auth_token_without_a_file_at_all(tmp_path):
    assert npm.clear_auth_token(tmp_path / ".npmrc") is False


def test_npm_whoami_command_matches_the_installed_package_manager(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path / "only-bun", "bun")
    assert npm.whoami_command() == "bun pm whoami"

    only_on_path(monkeypatch, tmp_path / "only-npm", "npm")
    assert npm.whoami_command() == "npm whoami"


def test_npm_whoami_command_refuses_without_npm_or_bun(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path)
    with pytest.raises(VommitError, match="Neither `npm` nor `bun`"):
        npm.whoami_command()


def test_verify_npm_token_accepts_a_successful_whoami(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "bun")
    runner = FakeRunner().reply("bun pm whoami", stdout="someone")

    npm.verify_token("abc123", runner)  # does not raise


def test_verify_npm_token_refuses_a_failed_whoami(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "bun")
    runner = FakeRunner().reply(
        "bun pm whoami", returncode=1, stderr="ENEEDAUTH: need auth"
    )

    with pytest.raises(VommitError, match="need auth"):
        npm.verify_token("abc123", runner)


def test_verify_npm_token_checks_via_env_not_npmrc(tmp_path, monkeypatch):
    """
    Checked before ever being written anywhere, the same shape
    `auth.verify_token` checks a PyPI token before `authenticate` stores it,
    so a rejected token cannot clobber whatever `npmrc` already had.
    """
    only_on_path(monkeypatch, tmp_path, "bun")
    runner = FakeRunner().reply("bun pm whoami", stdout="someone")

    npm.verify_token("the-new-token", runner)

    # both spellings, because npm and bun read one each and not the same one:
    # npm ignores NPM_CONFIG_TOKEN entirely and takes only the registry-scoped
    # key, bun is the mirror image. Setting just one lets npm fall back to
    # `npmrc`, so a bad token would verify against the credential already there
    assert runner.env_for("bun pm whoami") == {
        "NPM_CONFIG_TOKEN": "the-new-token",
        "npm_config_//registry.npmjs.org/:_authToken": "the-new-token",
    }
