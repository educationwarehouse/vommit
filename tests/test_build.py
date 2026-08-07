from pathlib import Path

import pytest

from src.vommit.build import build_warnings

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
    assert "src/my_other_name/__init__.py" in warning
    assert "module-name" in warning


def test_a_flat_layout_is_not_where_uv_build_looks(tmp_path):
    root = project(
        tmp_path,
        '[project]\nname = "mypkg"\nversion = "1.0.0"\n' + UV_BUILD_BACKEND,
        module="mypkg",
    )
    (warning,) = build_warnings(root, "uv build")
    assert "src/mypkg/__init__.py" in warning


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
    assert "maturin" in warning
    assert "commands.build" in warning


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
