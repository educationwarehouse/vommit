import pytest

from src.vommit.errors import VommitError
from src.vommit.shell import LocalRunner
from src.vommit.versioning import UvProject

from .conftest import FakeRunner


def project(sandbox) -> UvProject:
    return UvProject(runner=LocalRunner(), root=sandbox.work)


def test_current_version(sandbox):
    assert project(sandbox).current_version() == "0.1.0"


def test_current_version_without_a_pyproject(tmp_path):
    assert UvProject(runner=LocalRunner(), root=tmp_path).current_version() is None


def test_current_version_for_a_dynamic_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert UvProject(runner=LocalRunner(), root=tmp_path).current_version() is None


def test_preview_leaves_the_project_alone(sandbox):
    uv = project(sandbox)

    assert uv.preview_bump("minor") == "0.2.0"
    assert uv.preview_bump("minor", prerelease=True) == "0.2.0rc1"
    assert uv.preview_set("9.9.9") == "9.9.9"

    assert uv.current_version() == "0.1.0"
    assert not uv.lockfile.exists()


def test_apply_writes_the_version_and_the_lockfile(sandbox):
    uv = project(sandbox)

    assert uv.apply_bump("patch") == "0.1.1"
    assert uv.current_version() == "0.1.1"
    assert "0.1.1" in uv.lockfile.read_text()

    assert uv.apply_set("2.0.0") == "2.0.0"
    assert uv.current_version() == "2.0.0"


def test_uv_failure_is_reported(tmp_path):
    uv = UvProject(runner=LocalRunner(), root=tmp_path)

    with pytest.raises(VommitError, match="`uv version` failed"):
        uv.preview_bump("patch")


def test_unreadable_uv_output_is_reported(tmp_path):
    uv = UvProject(runner=FakeRunner(), root=tmp_path)

    with pytest.raises(VommitError, match="Could not read a version"):
        uv.preview_bump("patch")


def test_preview_does_not_pass_no_sync(sandbox):
    runner = FakeRunner().reply("uv", stdout='{"version": "1.0.0"}')
    uv = UvProject(runner=runner, root=sandbox.work)

    uv.preview_bump("minor")
    assert runner.ran("--dry-run --frozen")

    uv.apply_bump("minor")
    assert runner.ran("--no-sync")
