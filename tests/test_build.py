from pathlib import Path

import pytest

from src.vommit.build import (
    RECOMMENDED_UV_BUILD_REQUIREMENT,
    backend_warnings,
    build_warnings,
    command_warnings,
    project_warnings,
)
from src.vommit.config import Config

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
