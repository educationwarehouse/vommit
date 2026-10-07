"""
The version source for projects whose wheel is built from a crate.

The invariant these guard is narrow but the one that matters: whatever vommit
plans, tags and writes to the changelog has to be the version maturin ends up
putting on the wheel. maturin reads `Cargo.toml` and normalises SemVer to PEP
440, so the two spellings have to agree in both directions.
"""

import pytest

from src.vommit.errors import VommitError
from src.vommit.shell import LocalRunner
from src.vommit.versioning import (
    CargoProject,
    UvProject,
    to_semver,
    version_source,
)

from .conftest import FakeRunner

MATURIN_PYPROJECT = """
[project]
name = "crate"
dynamic = ["version"]

[build-system]
requires = ["maturin>=1.5,<2.0"]
build-backend = "maturin"
"""

CARGO = '[package]\nname = "crate"\nversion = "{version}"\nedition = "2021"\n'


def crate(root, version="3.10.27", pyproject=MATURIN_PYPROJECT):
    if pyproject is not None:
        (root / "pyproject.toml").write_text(pyproject)
    if version is not None:
        (root / "Cargo.toml").write_text(CARGO.format(version=version))
    return root


def cargo(root, runner=None) -> CargoProject:
    return CargoProject(runner=runner or LocalRunner(), root=root)


@pytest.mark.parametrize(
    "version, expected",
    [
        ("1.2.3", "1.2.3"),
        ("1.2", "1.2.0"),
        ("1", "1.0.0"),
        ("1.2.3rc1", "1.2.3-rc.1"),
        ("1.2.3b4", "1.2.3-beta.4"),
        ("1.2.3a2", "1.2.3-alpha.2"),
    ],
)
def test_to_cargo_version(version, expected):
    assert to_semver(version) == expected


@pytest.mark.parametrize(
    "version, complaint",
    [
        ("1!1.2.3", "an epoch"),
        ("1.2.3.post1", "a post-release"),
        ("1.2.3.dev1", "a dev-release"),
        ("1.2.3+local", "a local version"),
    ],
)
def test_to_cargo_version_refuses_what_cargo_cannot_hold(version, complaint):
    """
    `--version` accepts any PEP 440 string; the ones SemVer has no spelling for
    are refused before the write rather than published under another number.
    """
    with pytest.raises(VommitError, match=complaint):
        to_semver(version)


def test_to_cargo_version_refuses_a_version_that_is_not_one():
    with pytest.raises(VommitError, match="not a PEP 440 version"):
        to_semver("dynamic")


def test_current_version_is_the_cargo_version_in_pep_440(tmp_path):
    assert cargo(crate(tmp_path, "3.10.27")).current_version() == "3.10.27"
    assert cargo(crate(tmp_path, "1.2.3-rc.1")).current_version() == "1.2.3rc1"
    assert cargo(crate(tmp_path, "1.2.3-beta.4")).current_version() == "1.2.3b4"


def test_current_version_without_a_cargo_toml(tmp_path):
    assert cargo(tmp_path).current_version() is None


@pytest.mark.parametrize(
    "manifest", ['[package]\nname = "crate"\n', 'package = "crate"\n']
)
def test_current_version_without_a_version_to_read(tmp_path, manifest):
    (tmp_path / "Cargo.toml").write_text(manifest)
    assert cargo(tmp_path).current_version() is None


def test_preview_leaves_the_crate_alone(tmp_path):
    project = cargo(crate(tmp_path, "3.10.27"))

    assert project.preview_bump("patch") == "3.10.28"
    assert project.preview_bump("minor", prerelease=True) == "3.11.0rc1"
    assert project.preview_set("4.0.0a2") == "4.0.0a2"

    assert project.current_version() == "3.10.27"
    assert "3.10.27" in (tmp_path / "Cargo.toml").read_text()


def test_preview_needs_a_version_to_start_from(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "crate"\n')

    with pytest.raises(VommitError, match="No `\\[package\\].version`"):
        cargo(tmp_path).preview_bump("patch")


def test_apply_writes_semver_and_reports_pep_440(tmp_path):
    project = cargo(crate(tmp_path, "3.10.27"))

    assert project.apply_bump("minor", prerelease=True) == "3.11.0rc1"
    assert 'version = "3.11.0-rc.1"' in (tmp_path / "Cargo.toml").read_text()
    # and it reads back as the version that was reported
    assert project.current_version() == "3.11.0rc1"

    assert project.apply_set("4.0.0") == "4.0.0"
    assert 'version = "4.0.0"' in (tmp_path / "Cargo.toml").read_text()


def test_apply_keeps_the_rest_of_the_manifest(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '# a comment\n[package]\nname = "crate"\nversion = "1.0.0"\n\n'
        '[dependencies]\nserde = "1.0"\n'
    )
    cargo(tmp_path).apply_bump("patch")

    content = (tmp_path / "Cargo.toml").read_text()
    assert "# a comment" in content
    assert 'serde = "1.0"' in content


def test_apply_refuses_a_manifest_that_lost_its_package_table(tmp_path):
    """
    Only reachable by editing `Cargo.toml` between the preview and the write,
    which is exactly the case the guard is there for: the version that was
    planned is no longer the version this file describes.
    """

    class Clobbering(FakeRunner):
        def run(self, command, env=None):
            (tmp_path / "Cargo.toml").write_text("[workspace]\nmembers = []\n")
            return super().run(command, env)

    runner = Clobbering().reply("uv", stdout='{"version": "1.1.0"}')
    project = cargo(crate(tmp_path, "1.0.0"), runner=runner)

    with pytest.raises(VommitError, match=r"\[package\] must be a TOML table"):
        project.apply_bump("minor")


def test_apply_refuses_a_version_cargo_cannot_hold(tmp_path):
    project = cargo(crate(tmp_path, "1.0.0"))

    with pytest.raises(VommitError, match="a dev-release"):
        project.apply_set("2.0.0.dev1")
    # nothing was written
    assert project.current_version() == "1.0.0"


def test_apply_relocks_only_when_there_is_a_lockfile(tmp_path):
    runner = FakeRunner().reply("uv", stdout='{"version": "1.1.0"}')
    project = cargo(crate(tmp_path, "1.0.0"), runner=runner)

    project.apply_bump("minor")
    assert not runner.ran("cargo update")

    (tmp_path / "Cargo.lock").write_text("")
    project.apply_bump("minor")
    assert runner.ran("cargo update --workspace")


def test_apply_does_not_relock_when_the_lockfile_is_ignored(tmp_path):
    runner = FakeRunner().reply("uv", stdout='{"version": "1.1.0"}')
    project = cargo(crate(tmp_path, "1.0.0"), runner=runner)
    (tmp_path / "Cargo.lock").write_text("")

    project.apply_bump("minor", frozen=True)
    assert not runner.ran("cargo update")


def test_a_failed_relock_says_the_manifest_already_moved(tmp_path):
    runner = FakeRunner().reply("uv", stdout='{"version": "1.1.0"}')
    runner.reply("cargo update", returncode=1, stderr="no network")
    project = cargo(crate(tmp_path, "1.0.0"), runner=runner)
    (tmp_path / "Cargo.lock").write_text("")

    with pytest.raises(VommitError, match="could not carry it into Cargo.lock"):
        project.apply_bump("minor")


def test_version_in_reads_an_older_revision(tmp_path):
    project = cargo(crate(tmp_path, "1.0.0"))
    assert project.version_in(CARGO.format(version="0.9.0-rc.2")) == "0.9.0rc2"


def test_version_source_picks_the_crate_for_a_dynamic_maturin_project(tmp_path):
    source = version_source(LocalRunner(), crate(tmp_path))

    assert isinstance(source, CargoProject)
    assert source.manifest_name == "Cargo.toml"
    assert source.lockfile_name == "Cargo.lock"


def test_version_source_honors_a_configured_manifest_path(tmp_path):
    """
    maturin's `[tool.maturin].manifest-path` puts the crate below the project
    root; the version and its lockfile live with that crate, not at the root.
    """
    root = crate(tmp_path, version=None)
    (root / "pyproject.toml").write_text(
        MATURIN_PYPROJECT + '\n[tool.maturin]\nmanifest-path = "rust/Cargo.toml"\n'
    )
    rust = root / "rust"
    rust.mkdir()
    (rust / "Cargo.toml").write_text(CARGO.format(version="3.10.27"))
    (rust / "Cargo.lock").write_text("")

    runner = FakeRunner().reply("uv", stdout='{"version": "3.11.0"}')
    source = version_source(runner, root)

    assert isinstance(source, CargoProject)
    assert source.manifest == rust / "Cargo.toml"
    assert source.lockfile == rust / "Cargo.lock"

    source.apply_bump("minor")
    assert source.current_version() == "3.11.0"
    update = "cargo update --workspace"
    assert runner.ran(f"{update} --manifest-path {rust / 'Cargo.toml'}")


@pytest.mark.parametrize(
    "reason, pyproject, version",
    [
        ("no pyproject at all", None, "1.0.0"),
        (
            "a static version uv can bump itself",
            '[project]\nname = "crate"\nversion = "1.0.0"\n\n[build-system]\n'
            'requires = ["maturin"]\nbuild-backend = "maturin"\n',
            "1.0.0",
        ),
        (
            "dynamic, but built as a Python package",
            '[project]\nname = "crate"\ndynamic = ["version"]\n\n[build-system]\n'
            'requires = ["hatchling"]\nbuild-backend = "hatchling.build"\n',
            "1.0.0",
        ),
        (
            "no build-system to identify",
            '[project]\nname = "crate"\ndynamic = ["version"]\n',
            "1.0.0",
        ),
        ("maturin, but the crate states no version", MATURIN_PYPROJECT, None),
    ],
)
def test_version_source_keeps_pyproject_for_everything_else(
    tmp_path, reason, pyproject, version
):
    root = crate(tmp_path, version=version, pyproject=pyproject)
    assert isinstance(version_source(LocalRunner(), root), UvProject), reason


def test_version_source_ignores_a_vendored_crate(tmp_path):
    """
    A Python package that happens to carry a `Cargo.toml` still publishes what
    `[project].version` says, so that is still the version to move.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "1.0.0"\n'
    )
    (tmp_path / "Cargo.toml").write_text(CARGO.format(version="9.9.9"))

    source = version_source(LocalRunner(), tmp_path)
    assert isinstance(source, UvProject)
    assert source.current_version() == "1.0.0"
