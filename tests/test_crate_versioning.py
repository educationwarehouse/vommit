"""
Which file holds the version once a crate is released on its own, to
crates.io rather than as a wheel.

The arithmetic is `CargoProject`'s and tested there; what is new is telling a
plain crate apart from a Python package that carries one, and refusing to
guess when a crate and a `package.json` both state a version.
"""

import json

import pytest

from src.vommit.errors import VommitError
from src.vommit.shell import LocalRunner
from src.vommit.versioning import (
    VERSION_SOURCES,
    CargoProject,
    CrateProject,
    NpmProject,
    UvProject,
    version_source,
)

from .conftest import FakeRunner

STANZA = "[tool.vommit.git]\nenabled = true\n"

CARGO = '[package]\nname = "crate"\nversion = "{version}"\nedition = "2021"\n'

MATURIN = (
    '[project]\nname = "crate"\ndynamic = ["version"]\n\n'
    '[build-system]\nrequires = ["maturin>=1.5,<2.0"]\nbuild-backend = "maturin"\n'
)


def crate(root, version="1.4.0", pyproject=STANZA):
    if pyproject is not None:
        (root / "pyproject.toml").write_text(pyproject)
    (root / "Cargo.toml").write_text(CARGO.format(version=version))
    return root


def package_json(root, version="2.0.0"):
    (root / "package.json").write_text(json.dumps({"name": "pkg", "version": version}))
    return root


def choose(root, name):
    with (root / "pyproject.toml").open("a") as pyproject:
        pyproject.write(f'\n[tool.vommit]\nversion_source = "{name}"\n')
    return root


def test_a_crate_beside_a_vommit_stanza_releases_cargo_toml(tmp_path):
    source = version_source(LocalRunner(), crate(tmp_path))

    assert isinstance(source, CrateProject)
    assert source.manifest == tmp_path / "Cargo.toml"
    assert source.lockfile == tmp_path / "Cargo.lock"
    assert source.current_version() == "1.4.0"


def test_a_crate_without_any_pyproject_releases_cargo_toml(tmp_path):
    source = version_source(LocalRunner(), crate(tmp_path, pyproject=None))
    assert isinstance(source, CrateProject)


def test_a_crate_does_not_publish_to_pypi_but_a_maturin_wheel_does():
    assert CrateProject.publishes_to_pypi is False
    assert NpmProject.publishes_to_pypi is False
    assert CargoProject.publishes_to_pypi is True
    assert UvProject.publishes_to_pypi is True


@pytest.mark.parametrize(
    "reason, pyproject",
    [
        (
            "a dynamic version some Python backend fills in",
            (
                '[project]\nname = "pkg"\ndynamic = ["version"]\n\n[build-system]\n'
                'requires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
            ),
        ),
        ("a [project] table with no version at all", '[project]\nname = "pkg"\n'),
    ],
)
def test_a_python_package_carrying_a_crate_is_not_released_as_one(
    tmp_path, reason, pyproject
):
    root = crate(tmp_path, pyproject=pyproject)
    assert isinstance(version_source(LocalRunner(), root), UvProject), reason


def test_a_crate_without_a_version_is_not_a_crate_release(tmp_path):
    root = crate(tmp_path)
    (root / "Cargo.toml").write_text('[package]\nname = "crate"\n')
    assert isinstance(version_source(LocalRunner(), root), UvProject)


def test_a_crate_and_a_package_json_both_stating_a_version_is_refused(tmp_path):
    root = package_json(crate(tmp_path))

    with pytest.raises(VommitError) as caught:
        version_source(LocalRunner(), root)

    message = str(caught.value)
    assert "package.json and Cargo.toml" in message
    assert 'version_source = "Cargo.toml"' in message


def test_a_package_json_without_a_version_leaves_the_crate_alone(tmp_path):
    root = crate(tmp_path)
    (root / "package.json").write_text('{"name": "frontend", "private": true}')
    assert isinstance(version_source(LocalRunner(), root), CrateProject)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Cargo.toml", CrateProject),
        ("package.json", NpmProject),
        ("pyproject.toml", UvProject),
    ],
)
def test_version_source_settles_which_file_to_release(tmp_path, name, expected):
    root = choose(package_json(crate(tmp_path, pyproject="")), name)
    assert type(version_source(LocalRunner(), root)) is expected


def test_choosing_cargo_toml_for_a_maturin_project_keeps_it_a_wheel(tmp_path):
    """
    The setting says which file, not where it goes: a maturin crate still
    publishes a wheel to PyPI, at maturin's manifest path.
    """
    root = crate(
        tmp_path,
        pyproject=MATURIN + '\n[tool.maturin]\nmanifest-path = "Cargo.toml"\n',
    )
    choose(root, "Cargo.toml")

    source = version_source(LocalRunner(), root)

    assert type(source) is CargoProject
    assert source.publishes_to_pypi is True


def test_an_unknown_version_source_is_refused_rather_than_guessed(tmp_path):
    root = choose(crate(tmp_path, pyproject=""), "Cargo.lock")

    with pytest.raises(VommitError) as caught:
        version_source(LocalRunner(), root)

    assert "'Cargo.lock'" in str(caught.value)
    for name in VERSION_SOURCES:
        assert name in str(caught.value)


def test_a_bump_moves_the_crate_and_relocks_it(tmp_path):
    root = crate(tmp_path)
    (root / "Cargo.lock").write_text("")
    runner = FakeRunner().reply("uv", stdout='{"version": "1.5.0"}')

    source = version_source(runner, root)
    assert source.apply_bump("minor") == "1.5.0"

    assert source.current_version() == "1.5.0"
    assert runner.ran(f"cargo update --workspace --manifest-path {root / 'Cargo.toml'}")


# --- a workspace sharing one version -----------------------------------------

VIRTUAL = (
    '[workspace]\nmembers = ["a"]\nresolver = "2"\n\n'
    '[workspace.package]\nversion = "{version}"\nedition = "2021"\n'
)
MEMBER = '[package]\nname = "a"\nversion.workspace = true\nedition.workspace = true\n'


def workspace(root, version="0.1.0", manifest=VIRTUAL):
    (root / "pyproject.toml").write_text(STANZA)
    (root / "Cargo.toml").write_text(manifest.format(version=version))
    (root / "a").mkdir()
    (root / "a" / "Cargo.toml").write_text(MEMBER)
    return root


def test_a_virtual_workspace_releases_its_shared_version(tmp_path):
    source = version_source(LocalRunner(), workspace(tmp_path, "0.3.0-beta.1"))

    assert isinstance(source, CrateProject)
    assert source.is_workspace is True
    assert source.current_version() == "0.3.0b1"


def test_a_bump_moves_the_shared_version_and_leaves_members_inheriting_it(tmp_path):
    root = workspace(tmp_path)
    runner = FakeRunner().reply("uv", stdout='{"version": "0.2.0"}')

    version_source(runner, root).apply_bump("minor")

    manifest = (root / "Cargo.toml").read_text()
    assert '[workspace.package]\nversion = "0.2.0"' in manifest
    assert (root / "a" / "Cargo.toml").read_text() == MEMBER


def test_a_root_package_inheriting_the_version_moves_the_workspace_one(tmp_path):
    root = workspace(
        tmp_path,
        manifest='[package]\nname = "root"\nversion.workspace = true\n\n' + VIRTUAL,
    )
    runner = FakeRunner().reply("uv", stdout='{"version": "0.1.1"}')

    source = version_source(runner, root)
    source.apply_bump("patch")

    assert source.current_version() == "0.1.1"
    manifest = (root / "Cargo.toml").read_text()
    assert "version.workspace = true" in manifest
    assert '[workspace.package]\nversion = "0.1.1"' in manifest


def test_a_root_package_with_its_own_version_keeps_releasing_that(tmp_path):
    root = workspace(
        tmp_path,
        "9.9.9",
        manifest='[package]\nname = "root"\nversion = "1.0.0"\n\n' + VIRTUAL,
    )
    assert version_source(LocalRunner(), root).current_version() == "1.0.0"


def test_a_single_crate_is_not_a_workspace(tmp_path):
    assert version_source(LocalRunner(), crate(tmp_path)).is_workspace is False


def test_a_missing_manifest_is_not_a_workspace(tmp_path):
    assert CrateProject(runner=LocalRunner(), root=tmp_path).is_workspace is False
