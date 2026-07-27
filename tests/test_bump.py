from datetime import date

import pytest

from src.vommit.bump import BumpRequest, run_bump, select_level
from src.vommit.config import Config
from src.vommit.errors import VommitError
from src.vommit.shell import LocalRunner

from .conftest import Sandbox

TODAY = date(2023, 4, 10)


def bump(sandbox: Sandbox, notify=None, **kwargs):
    return run_bump(
        config=Config.from_pyproject(sandbox.work),
        runner=LocalRunner(),
        root=sandbox.work,
        request=BumpRequest(**kwargs),
        notify=notify or (lambda _: None),
        today=TODAY,
    )


def changelog_of(sandbox: Sandbox) -> str:
    return (sandbox.work / "CHANGELOG.md").read_text()


def version_of(sandbox: Sandbox) -> str:
    return (
        Config
        and (sandbox.work / "pyproject.toml")
        .read_text()
        .split('version = "', 1)[1]
        .split('"', 1)[0]
    )


def test_select_level():
    assert select_level() is None
    assert select_level(minor=True) == "minor"
    assert select_level(patch=True) == "patch"
    assert select_level(major=True) == "major"


def test_select_level_rejects_two_flags():
    with pytest.raises(VommitError, match="--major and --minor"):
        select_level(major=True, minor=True)


def test_explicit_version_rejects_a_level():
    with pytest.raises(VommitError, match="drop --minor"):
        BumpRequest(version="1.2.3", level="minor")


def test_explicit_version_rejects_prerelease():
    with pytest.raises(VommitError, match="prerelease"):
        BumpRequest(version="1.2.3", prerelease=True)


def test_bump_derives_the_level_from_the_commits(sandbox):
    sandbox.commits("feat(api): add endpoint", "fix: correct a bug")

    result = bump(sandbox)

    assert result.level == "minor"
    assert result.previous == "0.1.0"
    assert result.version == "0.2.0"
    assert version_of(sandbox) == "0.2.0"


def test_bump_writes_changelog_commit_and_tag(sandbox):
    sandbox.commits("feat(api): add endpoint", "fix: correct a bug")

    result = bump(sandbox)

    assert changelog_of(sandbox) == (
        "# Changelog\n\n"
        "<!-- next-version-placeholder -->\n\n"
        "## v0.2.0 (2023-04-10)\n\n"
        "### Feature\n"
        "* **api:** add endpoint\n\n"
        "### Bug Fix\n"
        "* correct a bug\n"
    )
    assert result.tag == "v0.2.0"
    assert sandbox.tags() == ["v0.2.0"]
    assert sandbox.log(1) == ["0.2.0"]
    assert sandbox.status() == []


def test_bump_commits_the_lockfile_too(sandbox):
    sandbox.commits("fix: correct a bug")

    bump(sandbox)

    tracked = sandbox.git("show", "--name-only", "--format=", "HEAD").out.splitlines()
    assert sorted(tracked) == ["CHANGELOG.md", "pyproject.toml", "uv.lock"]


def test_second_bump_stacks_a_new_entry_on_top(sandbox):
    sandbox.commits("fix: correct a bug")
    bump(sandbox)
    sandbox.commits("feat: something new")

    result = bump(sandbox)

    assert result.version == "0.2.0"
    body = changelog_of(sandbox)
    assert body.index("## v0.2.0") < body.index("## v0.1.1")


def test_bump_reports_nothing_to_do(sandbox):
    sandbox.commits("chore: housekeeping")

    assert bump(sandbox) is None


def test_bump_honours_an_explicit_level(sandbox):
    sandbox.commits("fix: correct a bug")

    assert bump(sandbox, level="major").version == "1.0.0"


def test_bump_honours_an_explicit_version(sandbox):
    sandbox.commits("chore: housekeeping")

    result = bump(sandbox, version="7.7.7")

    assert result.version == "7.7.7"
    assert result.level is None
    assert sandbox.tags() == ["v7.7.7"]


def test_bump_prerelease(sandbox):
    sandbox.commits("feat: something new")

    assert bump(sandbox, prerelease=True).version == "0.2.0rc1"


def test_breaking_change_bumps_major_and_gets_its_own_section(sandbox):
    sandbox.commits("feat!: drop python 3.12")

    result = bump(sandbox)

    assert result.version == "1.0.0"
    assert "### Breaking Change\n* drop python 3.12" in changelog_of(sandbox)


def test_noop_changes_nothing_at_all(sandbox):
    # regression: a dry run used to create CHANGELOG.md as a side effect.
    sandbox.commits("feat: something new")

    result = bump(sandbox, noop=True)

    assert result.noop is True
    assert result.version == "0.2.0"
    assert "## v0.2.0 (2023-04-10)" in result.entry
    assert not (sandbox.work / "CHANGELOG.md").exists()
    assert not (sandbox.work / "uv.lock").exists()
    assert version_of(sandbox) == "0.1.0"
    assert sandbox.tags() == []
    assert sandbox.status() == []


def test_a_missing_placeholder_stops_before_anything_is_written(sandbox):
    # regression: uv bumped pyproject.toml first, so this failure used to leave
    # a dirty tree at a version that was never released.
    sandbox.write("CHANGELOG.md", "# Changelog\n\nno marker here\n")
    sandbox.commit("feat: something new")

    with pytest.raises(VommitError, match="placeholder"):
        bump(sandbox)

    assert version_of(sandbox) == "0.1.0"
    assert sandbox.status() == []
    assert sandbox.tags() == []


def test_wrong_branch_stops_before_anything_is_written(sandbox):
    sandbox.git("checkout", "-b", "feature/x")
    sandbox.commits("feat: something new")

    with pytest.raises(VommitError, match="expected 'main'"):
        bump(sandbox)

    assert version_of(sandbox) == "0.1.0"


def test_being_behind_upstream_stops_the_bump(sandbox):
    sandbox.commits("feat: something new")
    sandbox.push_upstream("feat: upstream work")

    with pytest.raises(VommitError, match="behind"):
        bump(sandbox)

    assert version_of(sandbox) == "0.1.0"


def test_current_branch_config_still_checks_its_own_upstream(tmp_path):
    # regression: '<current>' produced the literal ref 'origin/<current>', the
    # rev-list failed, and the freshness check passed without a word.
    sandbox = Sandbox(tmp_path)
    sandbox.create(config_branch="<current>")
    sandbox.commits("feat: something new")
    sandbox.push_upstream("feat: upstream work")

    with pytest.raises(VommitError, match="behind 'origin/main'"):
        bump(sandbox)


def test_current_branch_config_releases_from_a_feature_branch(tmp_path):
    sandbox = Sandbox(tmp_path)
    sandbox.create(config_branch="<current>")
    sandbox.git("checkout", "-b", "feature/x")
    sandbox.commits("feat: something new")
    messages: list[str] = []

    result = bump(sandbox, notify=messages.append)

    assert result.version == "0.2.0"
    assert "No upstream branch 'origin/feature/x'" in messages[0]


def test_uncommitted_changes_block_the_bump(sandbox):
    sandbox.commits("feat: something new")
    sandbox.write(
        "pyproject.toml", (sandbox.work / "pyproject.toml").read_text() + "\n"
    )

    with pytest.raises(VommitError, match="Uncommitted changes in pyproject.toml"):
        bump(sandbox)

    assert version_of(sandbox) == "0.1.0"


def test_uncommitted_changes_can_be_allowed(sandbox):
    sandbox.commits("feat: something new")
    sandbox.write(
        "pyproject.toml", (sandbox.work / "pyproject.toml").read_text() + "\n"
    )

    assert bump(sandbox, allow_dirty=True).version == "0.2.0"


def test_an_existing_tag_stops_the_bump(sandbox):
    sandbox.commits("feat: something new")
    sandbox.git("tag", "v0.2.0")
    sandbox.git("tag", "-d", "v0.2.0")
    sandbox.git("tag", "v0.2.0", "HEAD~1")

    with pytest.raises(VommitError, match="already exists"):
        bump(sandbox)

    # caught up front, so there is no release commit left dangling without a tag
    assert version_of(sandbox) == "0.1.0"
    assert sandbox.log(1) == ["feat: something new"]
    assert sandbox.status() == []


def test_a_changelog_outside_the_project_is_staged_by_absolute_path(sandbox, tmp_path):
    elsewhere = tmp_path / "outside" / "NOTES.md"
    sandbox.write(
        "pyproject.toml",
        (sandbox.work / "pyproject.toml")
        .read_text()
        .replace('file = "CHANGELOG.md"', f'file = "{elsewhere}"'),
    )
    sandbox.commit("feat: something new")

    with pytest.raises(VommitError, match="stage the release changes"):
        bump(sandbox)

    # the changelog itself lives outside the repo, so it was still written
    assert "## v0.2.0" in elsewhere.read_text()


def test_a_failed_commit_does_not_leave_a_tag_behind(sandbox):
    # regression: the commit result was never checked, so the tag ended up on
    # the commit before the release.
    sandbox.commits("feat: something new")
    hook = sandbox.work / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    with pytest.raises(VommitError, match="staged but not committed"):
        bump(sandbox)

    assert sandbox.tags() == []
    assert sandbox.log(1) == ["feat: something new"]


def test_a_version_that_shifts_underneath_us_is_caught(sandbox, monkeypatch):
    sandbox.commits("feat: something new")
    monkeypatch.setattr(
        "src.vommit.versioning.UvProject.apply_bump", lambda *a, **kw: "9.9.9"
    )

    with pytest.raises(VommitError, match="changed underneath us"):
        bump(sandbox)


def test_git_disabled_still_bumps_and_writes_the_changelog(sandbox):
    sandbox.write(
        "pyproject.toml",
        (sandbox.work / "pyproject.toml")
        .read_text()
        .replace(
            "[tool.vommit.git]\nenabled = true", "[tool.vommit.git]\nenabled = false"
        ),
    )
    sandbox.commit("feat: something new")

    result = bump(sandbox)

    assert result.version == "0.2.0"
    assert result.tag is None
    assert result.commit_message is None
    assert "## v0.2.0" in changelog_of(sandbox)
    assert sandbox.tags() == []


def test_changelog_disabled_still_commits_and_tags(sandbox):
    sandbox.write(
        "pyproject.toml",
        (sandbox.work / "pyproject.toml")
        .read_text()
        .replace(
            "[tool.vommit.changelog]\nenabled = true",
            "[tool.vommit.changelog]\nenabled = false",
        ),
    )
    sandbox.commit("feat: something new")

    result = bump(sandbox)

    assert result.entry is None
    assert result.changelog_path is None
    assert not (sandbox.work / "CHANGELOG.md").exists()
    assert sandbox.tags() == ["v0.2.0"]


def test_tagging_and_committing_can_be_switched_off(sandbox):
    sandbox.write(
        "pyproject.toml",
        (sandbox.work / "pyproject.toml")
        .read_text()
        .replace('tag_format = "v{version}"', 'tag_format = ""')
        .replace('commit_format = "{version}"', 'commit_format = ""'),
    )
    sandbox.commit("feat: something new")

    result = bump(sandbox)

    assert result.tag is None
    assert result.commit_message is None
    assert sandbox.tags() == []
    # changes are staged, but committing them is the user's business now
    staged = sorted(line[3:] for line in sandbox.status())
    assert staged == ["CHANGELOG.md", "pyproject.toml", "uv.lock"]
    assert all(line[1] == " " for line in sandbox.status())
