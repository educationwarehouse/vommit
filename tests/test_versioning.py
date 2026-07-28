import pytest

from src.vommit.errors import VommitError
from src.vommit.shell import LocalRunner
from src.vommit.versioning import (
    UvProject,
    implied_level,
    is_prerelease,
    plan_bump,
)

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

    uv.apply_bump("minor", frozen=True)
    assert runner.ran("--no-sync --frozen")


@pytest.mark.parametrize(
    "version, expected",
    [
        ("1.0.0", False),
        ("1.0.0rc1", True),
        ("1.0.0a1", True),
        ("1.0.0b2", True),
        ("1.0.0.dev1", True),
        ("1.0.0.post1", False),
        ("1.0.0+local", False),
        ("not-a-version", False),
        (None, False),
    ],
)
def test_is_prerelease(version, expected):
    assert is_prerelease(version) is expected


@pytest.mark.parametrize(
    "release, expected",
    [((1, 0, 0), "major"), ((1, 2, 0), "minor"), ((1, 2, 3), "patch")],
)
def test_implied_level(release, expected):
    assert implied_level(release) == expected


def test_plan_bump_from_a_released_version():
    assert plan_bump("1.0.0", "minor") == ["--bump", "minor"]
    assert plan_bump("1.0.0", "minor", prerelease_token="rc") == [
        "--bump",
        "minor",
        "--bump",
        "rc",
    ]


def test_plan_bump_releases_the_version_a_prerelease_was_built_for():
    # uv's own `--bump minor` would say 1.2.0 here, skipping 1.1.0 entirely
    assert plan_bump("1.1.0rc1", "minor", baseline="1.0.0") == ["--bump", "stable"]
    assert plan_bump("1.1.0rc1", "patch", baseline="1.0.0") == ["--bump", "stable"]


def test_plan_bump_steps_a_prerelease_series_it_still_agrees_with():
    assert plan_bump("1.1.0rc1", "patch", baseline="1.0.0", prerelease_token="rc") == [
        "--bump",
        "rc",
    ]


def test_plan_bump_restarts_when_a_bigger_change_arrives():
    assert plan_bump("1.1.0rc1", "major", baseline="1.0.0") == ["--bump", "major"]
    assert plan_bump("1.0.1rc1", "minor", baseline="1.0.0") == ["--bump", "minor"]


def test_plan_bump_without_a_baseline_reads_the_prerelease_shape():
    # 0.2.0rc1 can only be a minor step, which covers a patch but not a major
    assert plan_bump("0.2.0rc1", "patch") == ["--bump", "stable"]
    assert plan_bump("0.2.0rc1", "minor") == ["--bump", "stable"]
    assert plan_bump("0.2.0rc1", "major") == ["--bump", "major"]


def test_plan_bump_refuses_to_reuse_a_released_number():
    # 1.0.0 is out; a 1.0.0rc1 left behind in pyproject.toml cannot become it
    assert plan_bump("1.0.0rc1", "patch", baseline="1.0.0") == ["--bump", "patch"]


def test_plan_bump_ignores_an_unreadable_current_version():
    assert plan_bump("dynamic", "patch") == ["--bump", "patch"]
    assert plan_bump(None, "patch") == ["--bump", "patch"]


def test_planned_prerelease_promotion_matches_uv(sandbox):
    uv = project(sandbox)
    uv.apply_set("1.1.0rc1")

    assert uv.preview(plan_bump("1.1.0rc1", "minor", baseline="1.0.0")) == "1.1.0"
    assert (
        uv.preview(plan_bump("1.1.0rc1", "minor", baseline="1.0.0", prerelease_token="rc"))
        == "1.1.0rc2"
    )
    assert uv.preview(plan_bump("1.1.0rc1", "major", baseline="1.0.0")) == "2.0.0"
