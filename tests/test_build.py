import json
from pathlib import Path

import pytest

from src.vommit.build import (
    BUN_BUILD,
    BUN_PUBLISH,
    FIXABLE_WARNING_IDS,
    NPM_BUILD,
    NPM_PUBLISH,
    RECOMMENDED_UV_BUILD_REQUIREMENT,
    WARNING_IDS,
    backend_warnings,
    build_warnings,
    clear_npm_auth_token,
    command_warnings,
    has_npm_auth_token,
    npm_token_advice,
    npm_token_instructions,
    npm_whoami_command,
    project_warnings,
    requested_fixes,
    suggested_commands,
    verify_npm_token,
    write_npm_auth_token,
)
from src.vommit.config import Config
from src.vommit.errors import VommitError
from src.vommit.shell import LocalRunner

from .conftest import FakeRunner

UV_BUILD_BACKEND = """
[build-system]
requires = ["uv_build>=0.11.3,<0.12.0"]
build-backend = "uv_build"
"""


def project(root: Path, body: str, module: str | None = "src/mypkg") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(body)
    if module:
        path = root / module
        path.mkdir(parents=True, exist_ok=True)
        (path / "__init__.py").write_text("")
    return root


def test_a_well_formed_uv_build_project_has_nothing_to_say(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    )
    assert build_warnings(root, "uv build") == []


def test_a_renamed_distribution_no_longer_matches_its_module(tmp_path):
    """
    The verified failure: uv_build derives `src/<name>/` from `[project].name`,
    so renaming one without the other breaks the build and nothing else.
    """
    root = project(
        tmp_path,
        '[project]\nname = "my-other-name"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
        module="src/my_pkg",
    )
    (warning,) = build_warnings(root, "uv build")
    assert warning.id == "uv-build-module-missing"
    assert "src/my_other_name/__init__.py" in warning.message
    assert "module-name" in warning.message


def test_a_flat_layout_is_not_where_uv_build_looks(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
        module="mypkg",
    )
    (warning,) = build_warnings(root, "uv build")
    assert "src/mypkg/__init__.py" in warning.message


def test_a_single_file_module_is_not_a_module_to_uv_build(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
        module=None,
    )
    (root / "src").mkdir()
    (root / "src" / "mypkg.py").write_text("x = 1\n")
    assert build_warnings(root, "uv build")


@pytest.mark.parametrize(
    "settings, module",
    [
        ('[tool.uv.build-backend]\nmodule-name = "my_pkg"\n', "src/my_pkg"),
        ('[tool.uv.build-backend]\nmodule-root = "lib"\n', "lib/mypkg"),
    ],
)
def test_the_backend_can_be_pointed_somewhere_else(tmp_path, settings, module):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND + settings,
        module=module,
    )
    assert build_warnings(root, "uv build") == []


def test_a_namespace_package_is_left_alone(tmp_path):
    """
    Namespace layouts deliberately have no `__init__.py`, so the check that
    looks for one would report every single one of them.
    """
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n'
        + UV_BUILD_BACKEND
        + "[tool.uv.build-backend]\nnamespace = true\n",
        module=None,
    )
    assert build_warnings(root, "uv build") == []


def test_a_dotted_module_name_is_left_alone(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n'
        + UV_BUILD_BACKEND
        + '[tool.uv.build-backend]\nmodule-name = "company.mypkg"\n',
        module=None,
    )
    assert build_warnings(root, "uv build") == []


def test_a_project_without_a_name_is_left_alone(tmp_path):
    root = project(tmp_path, '[project]\nversion = "1.0.0"\n' + UV_BUILD_BACKEND, None)
    assert build_warnings(root, "uv build") == []


def test_a_missing_build_system_builds_anyway(tmp_path):
    """
    `uv build` falls back to setuptools and succeeds, flat layout included, so
    there is nothing here worth warning about.
    """
    root = project(tmp_path, '[project]\nname = "mypkg"\nversion = "1.0.0"\n')
    assert build_warnings(root, "uv build") == []


def test_maturin_is_warned_about_because_uv_build_makes_one_wheel(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\ndynamic = ["version"]\n\n[build-system]\n'
        'requires = ["maturin>=1.5,<2.0"]\nbuild-backend = "maturin"\n',
        module=None,
    )
    (warning,) = build_warnings(root, "uv build")
    assert warning.id == "maturin-uv-build"
    assert "maturin" in warning.message
    assert "commands.build" in warning.message


def test_another_build_command_is_not_second_guessed(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "my-other-name"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
        module="src/my_pkg",
    )
    assert build_warnings(root, "./build.sh") == []
    assert build_warnings(root, "") == []


def test_a_directory_without_a_pyproject_is_not_examined(tmp_path):
    assert build_warnings(tmp_path, "uv build") == []


HATCHLING_BACKEND = """
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""


def config(**commands: str) -> Config:
    settled = Config.default()
    if commands:
        for key, value in commands.items():
            setattr(settled.commands, key, value)
    return settled


def ids(warnings) -> set[str]:
    return {warning.id for warning in warnings}


def test_a_hatchling_project_is_offered_the_swap_to_uv_build(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + HATCHLING_BACKEND,
    )
    (warning,) = backend_warnings(root)
    assert warning.id == "hatchling-backend"
    assert warning.fix

    warning.fix.apply()

    body = (root / "pyproject.toml").read_text()
    assert 'build-backend = "uv_build"' in body
    assert RECOMMENDED_UV_BUILD_REQUIREMENT in body
    assert backend_warnings(root) == []


@pytest.mark.parametrize(
    "extra, blocker",
    [
        ('[tool.hatch.build]\npackages = ["src/mypkg"]\n', "[tool.hatch.build]"),
        (
            '[tool.hatch.version]\npath = "src/mypkg/__about__.py"\n',
            "[tool.hatch.version]",
        ),
        (
            "[tool.hatch.metadata]\nallow-direct-references = true\n",
            "[tool.hatch.metadata]",
        ),
    ],
)
def test_hatch_specific_build_settings_block_the_swap(tmp_path, extra, blocker):
    """
    Nothing under [tool.hatch] shaping the artifact survives the swap, so the
    two-key rewrite is not offered: it would change what gets published.
    """
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + HATCHLING_BACKEND + extra,
    )
    (warning,) = backend_warnings(root)
    assert warning.fix is None
    assert blocker in warning.message


def test_a_dynamic_version_blocks_the_swap(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\ndynamic = ["version"]\n' + HATCHLING_BACKEND,
    )
    (warning,) = backend_warnings(root)
    assert warning.fix is None
    assert "dynamic" in warning.message


def test_an_extra_build_requirement_blocks_the_swap(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n[build-system]\n'
        'requires = ["hatchling", "hatch-vcs"]\nbuild-backend = "hatchling.build"\n',
    )
    (warning,) = backend_warnings(root)
    assert warning.fix is None
    assert "hatch-vcs" in warning.message


def test_a_module_uv_build_cannot_find_blocks_the_swap(tmp_path):
    """
    Swapping the backend on a layout uv_build does not recognise trades a
    working build for a broken one.
    """
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + HATCHLING_BACKEND,
        module="mypkg",
    )
    (warning,) = backend_warnings(root)
    assert warning.fix is None
    assert "src/mypkg/__init__.py" in warning.message


def test_a_uv_build_pin_outside_the_range_is_rewritten(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    )
    (warning,) = backend_warnings(root)
    assert warning.id == "uv-build-pin"
    assert "0.11.3" in warning.message

    warning.fix.apply()

    assert RECOMMENDED_UV_BUILD_REQUIREMENT in (root / "pyproject.toml").read_text()
    assert backend_warnings(root) == []


def test_a_missing_uv_build_requirement_is_added(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n[build-system]\n'
        'requires = []\nbuild-backend = "uv_build"\n',
    )
    (warning,) = backend_warnings(root)
    assert warning.id == "uv-build-pin"

    warning.fix.apply()

    assert backend_warnings(root) == []


def test_the_recommended_pin_says_nothing(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n[build-system]\n'
        f'requires = ["{RECOMMENDED_UV_BUILD_REQUIREMENT}"]\n'
        'build-backend = "uv_build"\n',
    )
    assert backend_warnings(root) == []


def test_another_backend_is_not_second_guessed(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n[build-system]\n'
        'requires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n',
    )
    assert backend_warnings(root) == []
    assert backend_warnings(tmp_path / "elsewhere") == []


def test_a_hatch_build_command_is_rewritten_to_uv(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    )
    settled = config(build="hatch build -c", publish="hatch publish")
    (warning,) = command_warnings(root, settled)
    assert warning.id == "hatch-build-command"

    warning.fix.apply()

    assert settled.commands.build == "uv build"
    assert settled.commands.publish == "uv publish"
    body = (root / "pyproject.toml").read_text()
    assert 'build = "uv build"' in body
    assert 'publish = "uv publish"' in body


def test_a_build_command_that_does_more_than_build_is_not_rewritten(tmp_path):
    """
    `hatch build` inside a longer command is still worth reporting; what the
    rest of the command is for is not ours to decide, so no fix is offered.
    """
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    )
    (warning,) = command_warnings(root, config(build="hatch build && ./sign.sh"))
    assert warning.id == "hatch-build-command"
    assert warning.fix is None
    assert "commands.build" in warning.message


def test_a_publish_command_of_its_own_is_left_alone(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    )
    settled = config(build="hatch build", publish="./publish.sh")
    (warning,) = command_warnings(root, settled)
    warning.fix.apply()

    assert settled.commands.build == "uv build"
    assert settled.commands.publish == "./publish.sh"


def test_ignored_ids_are_not_reported(tmp_path):
    """
    The whole point of the id: a project that has decided uv_build is not for it
    keeps releasing without being told so on every run.
    """
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + HATCHLING_BACKEND,
    )
    settled = config(build="hatch build")
    assert ids(project_warnings(root, settled)) == {
        "hatchling-backend",
        "hatch-build-command",
    }

    settled.ignoring("hatchling-backend").ignoring("hatch-build-command")
    assert project_warnings(root, settled) == []


def test_ignoring_does_not_leak_into_the_next_config():
    """
    The default list lives on the class; appending to it would silence the
    warning for every project configured later in the same process.
    """
    config().ignoring("hatchling-backend")
    assert Config.default().ignore == []


def test_a_project_without_a_commands_table_has_no_commands_to_judge(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + HATCHLING_BACKEND,
    )
    settled = Config.default()
    settled.commands = None

    assert command_warnings(root, settled) == []
    # the backend still is what it is; only the command checks go quiet
    assert ids(project_warnings(root, settled)) == {"hatchling-backend"}


def test_a_build_command_that_is_already_uv_is_left_alone(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    )
    assert command_warnings(root, config(build="uv build")) == []


@pytest.mark.parametrize(
    "body",
    [
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + HATCHLING_BACKEND,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    ],
)
def test_a_fix_gives_up_when_the_build_system_is_gone(tmp_path, body):
    """
    The fix re-reads the file it was offered on, so an edit in between (or a
    `[build-system]` that was never a table) must not have it write nonsense.
    """
    root = project(tmp_path, body)
    (warning,) = backend_warnings(root)
    (root / "pyproject.toml").write_text('[project]\nname = "mypkg"\n')

    warning.fix.apply()

    assert (root / "pyproject.toml").read_text() == '[project]\nname = "mypkg"\n'


def test_a_requires_that_is_not_a_list_is_replaced_wholesale(tmp_path):
    """
    `requires = "uv_build"` is a typo git accepts and pip does not; there is no
    entry to rewrite in it, so the pin replaces the value.
    """
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n[build-system]\n'
        'requires = "uv_build"\nbuild-backend = "uv_build"\n',
    )
    (warning,) = backend_warnings(root)
    assert warning.id == "uv-build-pin"
    assert "is missing" in warning.message

    warning.fix.apply()

    assert (
        f'requires = ["{RECOMMENDED_UV_BUILD_REQUIREMENT}"]'
        in (root / "pyproject.toml").read_text()
    )
    assert backend_warnings(root) == []


def test_an_unparsable_requirement_is_stepped_over(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n[build-system]\n'
        'requires = ["not a requirement!", "uv_build>=0.11.3,<0.12.0"]\n'
        'build-backend = "uv_build"\n',
    )
    (warning,) = backend_warnings(root)
    assert warning.id == "uv-build-pin"

    warning.fix.apply()

    body = (root / "pyproject.toml").read_text()
    assert '"not a requirement!"' in body
    assert RECOMMENDED_UV_BUILD_REQUIREMENT in body
    assert backend_warnings(root) == []


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
    only_on_path(monkeypatch, tmp_path, "npm", "bun")
    root = project(
        tmp_path / "root",
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    )
    assert suggested_commands(root, LocalRunner()) is None


def test_suggested_commands_is_none_when_nothing_states_a_version(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "npm", "bun")
    root = tmp_path / "root"
    root.mkdir()
    assert suggested_commands(root, LocalRunner()) is None


def test_suggested_commands_prefers_bun_when_both_are_installed(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "npm", "bun")
    root = npm_package(tmp_path / "root")
    suggestion = suggested_commands(root, LocalRunner())

    assert suggestion is not None
    assert suggestion.publish == BUN_PUBLISH


def test_suggested_commands_falls_back_to_npm_without_bun(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "npm")
    root = npm_package(tmp_path / "root")
    suggestion = suggested_commands(root, LocalRunner())

    assert suggestion is not None
    assert suggestion.publish == NPM_PUBLISH


def test_suggested_commands_refuses_to_guess_without_npm_or_bun(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path)
    root = npm_package(tmp_path / "root")

    with pytest.raises(VommitError, match="Neither `npm` nor `bun`"):
        suggested_commands(root, LocalRunner())


def test_suggested_commands_skips_build_by_default(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "bun")
    root = npm_package(tmp_path / "root")
    suggestion = suggested_commands(root, LocalRunner())

    assert suggestion is not None
    assert suggestion.build == ""


def test_suggested_commands_offers_a_build_step_when_the_script_exists(
    tmp_path, monkeypatch
):
    only_on_path(monkeypatch, tmp_path, "bun")
    root = npm_package(tmp_path / "root", scripts={"build": "tsc"})
    suggestion = suggested_commands(root, LocalRunner())

    assert suggestion is not None
    assert suggestion.build == BUN_BUILD


def test_suggested_commands_offers_the_matching_build_step_without_bun(
    tmp_path, monkeypatch
):
    only_on_path(monkeypatch, tmp_path, "npm")
    root = npm_package(tmp_path / "root", scripts={"build": "tsc"})
    suggestion = suggested_commands(root, LocalRunner())

    assert suggestion is not None
    assert suggestion.build == NPM_BUILD


def test_suggested_commands_ignores_a_blank_build_script(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "bun")
    root = npm_package(tmp_path / "root", scripts={"build": "   "})
    suggestion = suggested_commands(root, LocalRunner())

    assert suggestion is not None
    assert suggestion.build == ""


def test_suggested_commands_leaves_clean_and_post_publish_at_their_defaults(
    tmp_path, monkeypatch
):
    only_on_path(monkeypatch, tmp_path, "bun")
    root = npm_package(tmp_path / "root", scripts={"build": "tsc"})
    suggestion = suggested_commands(root, LocalRunner())

    assert suggestion is not None
    assert suggestion.clean == "rm -rf ./dist"
    assert suggestion.post_publish == ""


def test_has_npm_auth_token_is_false_without_an_npmrc(tmp_path):
    assert has_npm_auth_token(tmp_path / ".npmrc") is False


def test_has_npm_auth_token_is_false_for_an_unrelated_npmrc(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("registry=https://registry.npmjs.org/\nsave-exact=true\n")
    assert has_npm_auth_token(npmrc) is False


def test_has_npm_auth_token_is_true_once_a_token_is_configured(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("//registry.npmjs.org/:_authToken=abc123\n")
    assert has_npm_auth_token(npmrc) is True


def test_has_npm_auth_token_is_scoped_to_the_given_registry(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("//registry.example.com/:_authToken=abc123\n")
    assert has_npm_auth_token(npmrc) is False
    assert has_npm_auth_token(npmrc, registry="registry.example.com") is True


def test_has_npm_auth_token_ignores_leading_whitespace(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("  //registry.npmjs.org/:_authToken=abc123\n")
    assert has_npm_auth_token(npmrc) is True


def test_npm_token_instructions_names_the_registry_line_and_the_file(tmp_path):
    npmrc = tmp_path / ".npmrc"
    instructions = npm_token_instructions(npmrc, registry="registry.example.com")
    assert "//registry.example.com/:_authToken=<token>" in instructions
    assert str(npmrc) in instructions


def test_npm_token_instructions_includes_where_to_get_one():
    instructions = npm_token_instructions()
    assert "https://www.npmjs.com/settings/~/tokens" in instructions
    assert "Bypass two-factor authentication" in instructions


def test_npm_token_advice_names_the_url_and_the_setting_to_check():
    advice = npm_token_advice()
    assert "https://www.npmjs.com/settings/~/tokens" in advice
    assert "Bypass two-factor authentication" in advice


def test_npm_token_advice_warns_off_a_plain_login_token():
    advice = npm_token_advice()
    assert "npm login" in advice
    assert "does NOT count" in advice


def test_write_npm_auth_token_creates_a_new_file(tmp_path):
    npmrc = tmp_path / ".npmrc"
    write_npm_auth_token("abc123", npmrc)
    assert npmrc.read_text() == "//registry.npmjs.org/:_authToken=abc123\n"


def test_write_npm_auth_token_appends_to_an_existing_file(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("registry=https://registry.npmjs.org/\n")
    write_npm_auth_token("abc123", npmrc)
    assert npmrc.read_text() == (
        "registry=https://registry.npmjs.org/\n"
        "//registry.npmjs.org/:_authToken=abc123\n"
    )


def test_write_npm_auth_token_appends_a_missing_trailing_newline_first(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("registry=https://registry.npmjs.org/")
    write_npm_auth_token("abc123", npmrc)
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
    write_npm_auth_token("new-token", npmrc)
    assert npmrc.read_text() == (
        "always-auth=true\n"
        "//registry.npmjs.org/:_authToken=new-token\n"
        "save-exact=true\n"
    )


def test_write_npm_auth_token_only_replaces_the_matching_registry(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("//registry.example.com/:_authToken=other\n")
    write_npm_auth_token("abc123", npmrc, registry="registry.npmjs.org")
    assert npmrc.read_text() == (
        "//registry.example.com/:_authToken=other\n"
        "//registry.npmjs.org/:_authToken=abc123\n"
    )


def test_clear_npm_auth_token_removes_the_line_and_reports_it(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("always-auth=true\n//registry.npmjs.org/:_authToken=abc123\n")

    assert clear_npm_auth_token(npmrc) is True
    assert npmrc.read_text() == "always-auth=true\n"


def test_clear_npm_auth_token_reports_when_there_was_nothing_to_clear(tmp_path):
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("always-auth=true\n")

    assert clear_npm_auth_token(npmrc) is False
    assert npmrc.read_text() == "always-auth=true\n"


def test_clear_npm_auth_token_without_a_file_at_all(tmp_path):
    assert clear_npm_auth_token(tmp_path / ".npmrc") is False


def test_npm_whoami_command_matches_the_installed_package_manager(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path / "only-bun", "bun")
    assert npm_whoami_command() == "bun pm whoami"

    only_on_path(monkeypatch, tmp_path / "only-npm", "npm")
    assert npm_whoami_command() == "npm whoami"


def test_npm_whoami_command_refuses_without_npm_or_bun(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path)
    with pytest.raises(VommitError, match="Neither `npm` nor `bun`"):
        npm_whoami_command()


def test_verify_npm_token_accepts_a_successful_whoami(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "bun")
    runner = FakeRunner().reply("bun pm whoami", stdout="someone")

    verify_npm_token("abc123", runner)  # does not raise


def test_verify_npm_token_refuses_a_failed_whoami(tmp_path, monkeypatch):
    only_on_path(monkeypatch, tmp_path, "bun")
    runner = FakeRunner().reply(
        "bun pm whoami", returncode=1, stderr="ENEEDAUTH: need auth"
    )

    with pytest.raises(VommitError, match="need auth"):
        verify_npm_token("abc123", runner)


def test_verify_npm_token_checks_via_env_not_npmrc(tmp_path, monkeypatch):
    """
    Checked before ever being written anywhere, the same shape
    `auth.verify_token` checks a PyPI token before `authenticate` stores it,
    so a rejected token cannot clobber whatever `npmrc` already had.
    """
    only_on_path(monkeypatch, tmp_path, "bun")
    runner = FakeRunner().reply("bun pm whoami", stdout="someone")

    verify_npm_token("the-new-token", runner)

    assert runner.env_for("bun pm whoami") == {"NPM_CONFIG_TOKEN": "the-new-token"}
def test_every_warning_id_is_registered(tmp_path):
    """
    `--fix` and `ignore` both check ids against the registry, so an id that is
    produced but not listed there is refused by a flag that should accept it.
    """
    hatchling = project(
        tmp_path / "hatchling",
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + HATCHLING_BACKEND,
    )
    maturin = project(
        tmp_path / "maturin",
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n'
        '[build-system]\nrequires = ["maturin>=1"]\nbuild-backend = "maturin"\n',
    )
    missing = project(
        tmp_path / "missing",
        '[project]\nname = "other"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    )

    produced = {
        warning.id
        for root in (hatchling, maturin, missing)
        for warning in project_warnings(root, config(build="uv build"))
    } | {"hatch-build-command"}

    assert produced == WARNING_IDS


def test_no_fix_is_named_fixable_without_one(tmp_path):
    """
    Every id in FIXABLE_WARNING_IDS has to be able to produce a `fix`, or the
    flag advertises a change it can never make.
    """
    hatchling = project(
        tmp_path / "hatchling",
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + HATCHLING_BACKEND,
    )
    pinned = project(
        tmp_path / "pinned",
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
    )

    offered = {
        warning.id
        for root in (hatchling, pinned)
        for warning in project_warnings(root, config(build="hatch build"))
        if warning.fix
    }

    assert offered == FIXABLE_WARNING_IDS


def test_nothing_asked_for_fixes_nothing():
    assert requested_fixes(None) == set()


def test_all_asks_for_every_fixable_id():
    assert requested_fixes("all") == FIXABLE_WARNING_IDS


@pytest.mark.parametrize(
    "flag",
    [
        "hatchling-backend",
        " hatchling-backend , hatch-build-command ",
        "hatch-build-command,hatchling-backend,",
    ],
)
def test_ids_are_taken_as_a_comma_separated_list(flag):
    assert requested_fixes(flag) <= FIXABLE_WARNING_IDS
    assert "hatchling-backend" in requested_fixes(flag)


def test_a_misspelled_id_is_refused_rather_than_ignored():
    """
    A silent no-op here is indistinguishable from a project with nothing to fix,
    which is the one outcome an unattended run cannot notice.
    """
    with pytest.raises(VommitError, match="hatchling-backends"):
        requested_fixes("hatchling-backends")


def test_a_real_id_with_no_fix_is_refused():
    with pytest.raises(VommitError, match="maturin-uv-build"):
        requested_fixes("maturin-uv-build")


def test_an_empty_flag_is_refused():
    with pytest.raises(VommitError, match="needs a warning id"):
        requested_fixes(" , ")
