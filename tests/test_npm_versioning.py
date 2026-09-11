"""
The version source for a project that keeps its version in `package.json`.

npm, pnpm, yarn and bun all read the same field, and all speak plain SemVer,
so the invariant that matters is the same one `CargoProject` guards: whatever
vommit plans and tags has to be the exact string that ends up written back.
"""

import json

import pytest

from src.vommit.errors import VommitError
from src.vommit.shell import LocalRunner
from src.vommit.versioning import CargoProject, NpmProject, UvProject, version_source

from .conftest import FakeRunner


def package(root, version="3.10.27", extra=None):
    data = {"name": "pkg", **(extra or {})}
    if version is not None:
        data["version"] = version
    (root / "package.json").write_text(json.dumps(data, indent=2) + "\n")
    return root


def npm(root, runner=None) -> NpmProject:
    return NpmProject(runner=runner or LocalRunner(), root=root)


def test_current_version_is_read_from_package_json(tmp_path):
    assert npm(package(tmp_path, "3.10.27")).current_version() == "3.10.27"
    assert npm(package(tmp_path, "1.2.3-rc.1")).current_version() == "1.2.3rc1"
    assert npm(package(tmp_path, "1.2.3-beta.4")).current_version() == "1.2.3b4"


def test_current_version_without_a_package_json(tmp_path):
    assert npm(tmp_path).current_version() is None


def test_current_version_without_a_version_to_read(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "pkg"}\n')
    assert npm(tmp_path).current_version() is None


def test_current_version_ignores_a_package_json_that_is_not_an_object(tmp_path):
    (tmp_path / "package.json").write_text("[]\n")
    assert npm(tmp_path).current_version() is None


def test_current_version_ignores_a_package_json_that_is_not_valid_json(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    assert npm(tmp_path).current_version() is None


def test_current_version_ignores_a_version_semver_cannot_parse(tmp_path):
    # a pnpm/yarn workspace protocol reference, not a real version
    (tmp_path / "package.json").write_text('{"name": "pkg", "version": "workspace:*"}\n')
    assert npm(tmp_path).current_version() is None


def test_preview_leaves_the_manifest_alone(tmp_path):
    project = npm(package(tmp_path, "3.10.27"))

    assert project.preview_bump("patch") == "3.10.28"
    assert project.preview_bump("minor", prerelease=True) == "3.11.0rc1"
    assert project.preview_set("4.0.0a2") == "4.0.0a2"

    assert project.current_version() == "3.10.27"
    assert '"version": "3.10.27"' in (tmp_path / "package.json").read_text()


def test_preview_needs_a_version_to_start_from(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "pkg"}\n')

    with pytest.raises(VommitError, match="No `.version`"):
        npm(tmp_path).preview_bump("patch")


def test_apply_writes_semver_and_reports_pep_440(tmp_path):
    project = npm(package(tmp_path, "3.10.27"))

    assert project.apply_bump("minor", prerelease=True) == "3.11.0rc1"
    assert '"version": "3.11.0-rc.1"' in (tmp_path / "package.json").read_text()
    # and it reads back as the version that was reported
    assert project.current_version() == "3.11.0rc1"

    assert project.apply_set("4.0.0") == "4.0.0"
    assert '"version": "4.0.0"' in (tmp_path / "package.json").read_text()


def test_apply_keeps_the_rest_of_the_manifest(tmp_path):
    package(
        tmp_path,
        "1.0.0",
        extra={"description": "a package", "dependencies": {"left-pad": "1.0.0"}},
    )
    npm(tmp_path).apply_bump("patch")

    document = json.loads((tmp_path / "package.json").read_text())
    assert document["description"] == "a package"
    assert document["dependencies"] == {"left-pad": "1.0.0"}
    assert document["version"] == "1.0.1"


def test_apply_refuses_a_manifest_that_lost_its_object_shape(tmp_path):
    """
    Only reachable by editing `package.json` between the preview and the
    write, which is exactly the case the guard is there for: the version that
    was planned is no longer the version this file describes.
    """

    class Clobbering(FakeRunner):
        def run(self, command, env=None):
            (tmp_path / "package.json").write_text("[]\n")
            return super().run(command, env)

    runner = Clobbering().reply("uv", stdout='{"version": "1.1.0"}')
    project = npm(package(tmp_path, "1.0.0"), runner=runner)

    with pytest.raises(VommitError, match="must contain a JSON object"):
        project.apply_bump("minor")


def test_apply_refuses_a_version_semver_cannot_hold(tmp_path):
    project = npm(package(tmp_path, "1.0.0"))

    with pytest.raises(VommitError, match="a dev-release"):
        project.apply_set("2.0.0.dev1")
    # nothing was written
    assert project.current_version() == "1.0.0"


def test_apply_never_touches_a_lockfile(tmp_path):
    """
    Deliberate: see `NpmProject`'s docstring. A stale lockfile is left alone
    rather than staged unchanged into a release commit that just moved the
    version it claims to lock.
    """
    package(tmp_path, "1.0.0")
    (tmp_path / "package-lock.json").write_text('{"version": "1.0.0"}\n')

    project = npm(tmp_path)
    project.apply_bump("minor")

    assert not project.lockfile.exists()
    assert (tmp_path / "package-lock.json").read_text() == '{"version": "1.0.0"}\n'


def test_version_in_reads_an_older_revision(tmp_path):
    project = npm(package(tmp_path, "1.0.0"))
    assert (
        project.version_in(json.dumps({"name": "pkg", "version": "0.9.0-rc.2"}))
        == "0.9.0rc2"
    )


def test_version_source_picks_package_json_when_pyproject_has_no_version(tmp_path):
    """
    The shape a JS-only project takes: a `pyproject.toml` that holds nothing
    but `[tool.vommit]` config, and the real version in `package.json`.
    """
    (tmp_path / "pyproject.toml").write_text("[tool.vommit]\n")
    package(tmp_path, "1.2.3")

    source = version_source(LocalRunner(), tmp_path)
    assert isinstance(source, NpmProject)
    assert source.manifest_name == "package.json"
    assert source.current_version() == "1.2.3"


def test_version_source_picks_package_json_with_no_pyproject_at_all(tmp_path):
    package(tmp_path, "1.2.3")
    assert isinstance(version_source(LocalRunner(), tmp_path), NpmProject)


def test_version_source_prefers_a_real_python_project_over_a_vendored_package_json(
    tmp_path,
):
    """
    A Python package that bundles frontend tooling still publishes what
    `[project].version` says, the same principle as a vendored `Cargo.toml`.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "1.0.0"\n'
    )
    package(tmp_path, "9.9.9")

    source = version_source(LocalRunner(), tmp_path)
    assert isinstance(source, UvProject)
    assert source.current_version() == "1.0.0"


def test_version_source_prefers_a_dynamic_maturin_project_over_package_json(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "crate"\ndynamic = ["version"]\n\n[build-system]\n'
        'requires = ["maturin>=1.5,<2.0"]\nbuild-backend = "maturin"\n'
    )
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "crate"\nversion = "3.10.27"\nedition = "2021"\n'
    )
    package(tmp_path, "9.9.9")

    assert isinstance(version_source(LocalRunner(), tmp_path), CargoProject)


def test_version_source_falls_back_to_uv_project_when_nothing_states_a_version(
    tmp_path,
):
    """
    No `pyproject.toml`, no `package.json`: same "nothing to release" shape as
    today, reported against `pyproject.toml` as before, not `package.json`.
    """
    source = version_source(LocalRunner(), tmp_path)
    assert isinstance(source, UvProject)
    assert source.current_version() is None
