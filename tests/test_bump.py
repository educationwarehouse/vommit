import datetime as dt
import pytest

from src.vommit.bump import BumpRequest, run_bump, select_level
from src.vommit.config import Config
from src.vommit.errors import VommitError
from src.vommit.shell import LocalRunner

from .conftest import Sandbox

TODAY = dt.date(2023, 4, 10)


def bump(sandbox: Sandbox, notify=None, confirm=None, **kwargs):
    return run_bump(
        config=Config.from_pyproject(sandbox.work),
        runner=LocalRunner(),
        root=sandbox.work,
        request=BumpRequest(**kwargs),
        notify=notify or (lambda _: None),
        today=TODAY,
        **({"confirm": confirm} if confirm else {}),
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
        "### Fix\n"
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


def test_bump_does_not_create_an_ignored_lockfile(sandbox):
    sandbox.write(".gitignore", "uv.lock\n")
    sandbox.commit("chore: ignore the lockfile")
    sandbox.commits("fix: correct a bug")

    bump(sandbox)

    assert not (sandbox.work / "uv.lock").exists()
    assert sandbox.status() == []


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


def test_prerelease_stays_out_of_the_changelog(sandbox):
    sandbox.commits("feat: something new")

    result = bump(sandbox, prerelease=True)

    assert result.prerelease is True
    assert result.entry is None
    assert result.changelog_path is None
    assert not (sandbox.work / "CHANGELOG.md").exists()
    # the version itself is still bumped, committed and tagged
    assert sandbox.tags() == ["v0.2.0rc1"]


def test_prerelease_says_why_it_wrote_no_entry(sandbox):
    sandbox.commits("feat: something new")
    said: list[str] = []

    bump(sandbox, prerelease=True, notify=said.append)

    assert any("stay unlisted" in message for message in said)


def test_release_collects_everything_since_the_last_release(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox, prerelease=True)
    sandbox.commits("fix: correct a bug")

    result = bump(sandbox)

    # the rc series was building towards 0.2.0, so that is where it lands
    assert result.version == "0.2.0"
    entry = changelog_of(sandbox)
    assert "## v0.2.0 (2023-04-10)" in entry
    # both the prerelease's commit and the one after it
    assert "* something new" in entry
    assert "* correct a bug" in entry
    assert "0.2.0rc1" not in entry


def test_release_promotes_a_prerelease_with_nothing_new_behind_it(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox, prerelease=True)

    # no commits since the rc: releasing it is still the point
    result = bump(sandbox)

    assert result.version == "0.2.0"
    assert "* something new" in changelog_of(sandbox)


def test_a_second_prerelease_steps_the_series(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox, prerelease=True)
    sandbox.commits("fix: correct a bug")

    # not 0.2.1rc1: the release this series is heading for has not changed
    assert bump(sandbox, prerelease=True).version == "0.2.0rc2"


def test_a_breaking_change_abandons_the_prerelease_series(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox, prerelease=True)
    sandbox.commits("feat!: drop python 3.12")

    result = bump(sandbox)

    assert result.version == "1.0.0"
    assert "* something new" in changelog_of(sandbox)


def test_including_prereleases_gives_each_one_its_own_entry(sandbox):
    sandbox.set_config("tool.vommit.changelog", include_prereleases=True)
    sandbox.commits("feat: something new")

    result = bump(sandbox, prerelease=True)

    assert result.prerelease is True
    assert "## v0.2.0rc1 (2023-04-10)" in changelog_of(sandbox)
    assert result.entry is not None


def test_a_configured_commit_author_reaches_the_release_commit(sandbox):
    sandbox.set_config("tool.vommit.git", commit_author="Release Bot <bot@example.com>")
    sandbox.commits("feat: something new")

    bump(sandbox)

    assert sandbox.git("log", "-1", "--format=%an <%ae>").out == (
        "Release Bot <bot@example.com>"
    )


def test_a_malformed_commit_author_stops_before_the_version_changes(sandbox):
    """
    The check has to come first: git only refuses the author at commit time,
    which is after `uv version` has rewritten pyproject.toml.
    """
    sandbox.set_config("tool.vommit.git", commit_author="Release Bot")
    sandbox.commits("feat: something new")
    before = (sandbox.work / "pyproject.toml").read_text()

    with pytest.raises(VommitError, match="git takes 'Name <email>'"):
        bump(sandbox)

    assert (sandbox.work / "pyproject.toml").read_text() == before
    assert sandbox.tags() == []


def test_including_prereleases_keeps_the_window_at_the_last_tag(sandbox):
    sandbox.set_config("tool.vommit.changelog", include_prereleases=True)
    sandbox.commits("feat: something new")
    bump(sandbox, prerelease=True)
    sandbox.commits("fix: correct a bug")

    bump(sandbox)

    entry = changelog_of(sandbox)
    # the feat was already listed under the rc, so the release only adds the fix
    assert entry.index("* correct a bug") < entry.index("* something new")
    assert entry.count("* something new") == 1


@pytest.mark.parametrize(
    "token, expected", [("alpha", "0.2.0a1"), ("beta", "0.2.0b1"), ("rc", "0.2.0rc1")]
)
def test_the_prerelease_token_is_configurable(sandbox, token, expected):
    sandbox.set_config("tool.vommit", prerelease_token=token)
    sandbox.commits("feat: something new")

    assert bump(sandbox, prerelease=True).version == expected


def test_a_quiet_prerelease_series_is_releasable_without_a_changelog(sandbox):
    sandbox.set_config("tool.vommit.changelog", enabled=False)
    sandbox.commits("feat: something new")
    bump(sandbox, prerelease=True)

    # nothing new since the rc, but promoting it is still a real bump
    assert bump(sandbox).version == "0.2.0"


def test_an_explicit_prerelease_version_is_treated_as_one(sandbox):
    sandbox.commits("feat: something new")

    result = bump(sandbox, version="1.0.0rc1")

    assert result.prerelease is True
    assert result.entry is None


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
        "src.vommit.versioning.UvProject.apply", lambda *a, **kw: "9.9.9"
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


def test_a_declined_bump_writes_nothing(sandbox):
    sandbox.commits("feat: something new")

    result = bump(sandbox, confirm=lambda _: False)

    assert result.cancelled is True
    assert result.noop is True
    assert result.version == "0.2.0"
    assert version_of(sandbox) == "0.1.0"
    assert sandbox.tags() == []
    assert not (sandbox.work / "CHANGELOG.md").exists()
    assert sandbox.status() == []


def test_confirmation_sees_the_release_it_is_asked_about(sandbox):
    sandbox.commits("feat: something new")
    asked: list[str] = []

    bump(sandbox, confirm=lambda result: bool(asked.append(result.version)) or True)

    assert asked == ["0.2.0"]
    assert version_of(sandbox) == "0.2.0"


def test_noop_never_asks(sandbox):
    sandbox.commits("feat: something new")

    def refuse(_):  # pragma: no cover - must not be reached
        raise AssertionError("a dry run has nothing to confirm")

    assert bump(sandbox, noop=True, confirm=refuse).noop is True
