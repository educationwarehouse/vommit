import datetime as dt

import pytest

from src.vommit.bump import BumpRequest, run_bump
from src.vommit.config import Config
from src.vommit.errors import VommitError
from src.vommit.shell import LocalRunner
from src.vommit.undo import UndoResult, run_undo

from .conftest import Sandbox

TODAY = dt.date(2023, 4, 10)


def bump(sandbox: Sandbox, **kwargs):
    return run_bump(
        config=Config.from_pyproject(sandbox.work),
        runner=LocalRunner(),
        root=sandbox.work,
        request=BumpRequest(**kwargs),
        today=TODAY,
    )


def undo(sandbox: Sandbox, notify=None, confirm=None, **kwargs) -> UndoResult:
    return run_undo(
        config=Config.from_pyproject(sandbox.work),
        runner=LocalRunner(),
        root=sandbox.work,
        notify=notify or (lambda _: None),
        **({"confirm": confirm} if confirm else {}),
        **kwargs,
    )


def version_of(sandbox: Sandbox) -> str | None:
    from src.vommit.versioning import UvProject

    return UvProject(runner=LocalRunner(), root=sandbox.work).current_version()


def changelog_of(sandbox: Sandbox) -> str:
    return (sandbox.work / "CHANGELOG.md").read_text()


def test_undo_rejects_a_bump_level():
    with pytest.raises(VommitError, match="drop --minor"):
        BumpRequest(undo=True, level="minor")


def test_undo_rejects_every_flag_that_aims_at_a_new_version():
    with pytest.raises(VommitError, match="--prerelease and --version"):
        BumpRequest(undo=True, prerelease=True, version="1.2.3")

    with pytest.raises(VommitError, match="--allow-dirty"):
        BumpRequest(undo=True, allow_dirty=True)


def test_undo_allows_noop_alongside():
    assert BumpRequest(undo=True, noop=True).undo is True


def test_undo_rewinds_a_committed_release(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox)
    before = sandbox.git("rev-parse", "HEAD~1").out

    result = undo(sandbox)

    assert result.plan.version == "0.2.0"
    assert result.plan.previous == "0.1.0"
    assert result.plan.tag == "v0.2.0"
    assert result.plan.commit == "0.2.0"
    assert sandbox.git("rev-parse", "HEAD").out == before
    assert sandbox.tags() == []
    assert version_of(sandbox) == "0.1.0"
    assert not (sandbox.work / "CHANGELOG.md").exists()
    assert sandbox.status() == []


def test_undo_leaves_earlier_releases_alone(sandbox):
    sandbox.commits("fix: correct a bug")
    bump(sandbox)
    sandbox.commits("feat: something new")
    bump(sandbox)

    undo(sandbox)

    assert version_of(sandbox) == "0.1.1"
    assert sandbox.tags() == ["v0.1.1"]
    body = changelog_of(sandbox)
    assert "## v0.1.1" in body
    assert "## v0.2.0" not in body


def test_undo_reports_what_it_did(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox)
    said: list[str] = []

    undo(sandbox, notify=said.append)

    assert said == ["Rewound to 0.1.0."]


def test_undo_noop_touches_nothing(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox)

    result = undo(sandbox, noop=True)

    assert result.noop is True
    assert result.plan.previous == "0.1.0"
    assert version_of(sandbox) == "0.2.0"
    assert sandbox.tags() == ["v0.2.0"]


def test_undo_can_be_declined(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox)

    result = undo(sandbox, confirm=lambda _: False)

    assert result.cancelled is True
    assert result.noop is True
    assert version_of(sandbox) == "0.2.0"
    assert sandbox.tags() == ["v0.2.0"]


def test_undo_refuses_a_pushed_tag(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox)
    sandbox.git("push", "origin", "v0.2.0")

    with pytest.raises(VommitError, match="has been pushed"):
        undo(sandbox)

    assert sandbox.tags() == ["v0.2.0"]


def test_undo_refuses_a_pushed_commit(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox)
    sandbox.git("push", "origin", "main")

    with pytest.raises(VommitError, match="already on origin/main"):
        undo(sandbox)


def test_undo_without_a_version_to_read(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    with pytest.raises(VommitError, match="nothing to undo"):
        run_undo(config=Config.default(), runner=LocalRunner(), root=tmp_path)


def test_undo_refuses_when_the_previous_version_is_the_same(sandbox):
    # nothing was released, so HEAD already carries the current version
    with pytest.raises(VommitError, match="no release here to undo"):
        undo(sandbox)


def test_undo_reports_an_unreadable_previous_pyproject(tmp_path):
    # a repo whose very first commit is the release: there is no revision behind
    # it to read a version from
    runner = LocalRunner()
    for command in (
        f"git init --initial-branch=main {tmp_path}",
        f"git -C {tmp_path} config user.email t@example.com",
        f"git -C {tmp_path} config user.name t",
    ):
        runner.run(command)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.2.0"\n'
    )
    runner.run(f"git -C {tmp_path} add -A")
    runner.run(f"git -C {tmp_path} commit -m 0.2.0")

    with pytest.raises(VommitError, match="nothing to go back to"):
        run_undo(config=Config.default(), runner=runner, root=tmp_path)


def test_undo_restores_files_when_nothing_was_committed(sandbox):
    sandbox.write(
        "pyproject.toml",
        (sandbox.work / "pyproject.toml")
        .read_text()
        .replace('tag_format = "v{version}"', 'tag_format = ""')
        .replace('commit_format = "{version}"', 'commit_format = ""'),
    )
    sandbox.commit("chore: stop committing releases")
    sandbox.commits("feat: something new")
    bump(sandbox)

    result = undo(sandbox)

    assert result.plan.commit is None
    assert result.plan.tag is None
    assert result.plan.changelog_path == sandbox.work / "CHANGELOG.md"
    assert version_of(sandbox) == "0.1.0"
    # the entry is gone; the file the release created stays, empty of entries
    assert changelog_of(sandbox) == "# Changelog\n\n<!-- next-version-placeholder -->\n"
    # and nothing the release staged is staged any more
    assert sandbox.status() == ["?? CHANGELOG.md", "?? uv.lock"]


def test_undo_removes_only_the_entry_it_wrote(sandbox):
    sandbox.write(
        "pyproject.toml",
        (sandbox.work / "pyproject.toml")
        .read_text()
        .replace('commit_format = "{version}"', 'commit_format = ""'),
    )
    sandbox.write(
        "CHANGELOG.md",
        "# Changelog\n\n<!-- next-version-placeholder -->\n\n"
        "## v0.0.9 (2020-01-01)\n\n### Fix\n* ancient\n",
    )
    sandbox.commit("chore: hand-written history")
    sandbox.commits("feat: something new")
    bump(sandbox)

    undo(sandbox)

    body = changelog_of(sandbox)
    assert "* ancient" in body
    assert "0.2.0" not in body


def test_undo_of_a_prerelease_has_no_entry_to_remove(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox, prerelease=True)

    result = undo(sandbox)

    # the prerelease never got a changelog entry, so there is none to take out
    assert result.plan.changelog_path is None
    assert version_of(sandbox) == "0.1.0"
    assert sandbox.tags() == []


def test_undo_ignores_a_tag_that_points_somewhere_else(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox)
    # move the tag off the release commit; undo must not delete it blindly
    sandbox.git("tag", "-f", "v0.2.0", "HEAD~1")

    result = undo(sandbox)

    assert result.plan.tag is None
    assert sandbox.tags() == ["v0.2.0"]


def test_undo_without_git_configured(sandbox):
    sandbox.set_config("tool.vommit.git", enabled=False)
    sandbox.commits("feat: something new")
    bump(sandbox)

    result = undo(sandbox)

    assert result.plan.tag is None
    assert result.plan.commit is None
    assert version_of(sandbox) == "0.1.0"


def test_rewinding_touches_no_individual_paths(sandbox):
    sandbox.commits("feat: something new")
    bump(sandbox)

    # a reset restores the whole tree, so there is nothing to unstage by name
    assert undo(sandbox, noop=True).plan.paths == ()


def test_undo_with_the_changelog_switched_off(sandbox):
    sandbox.set_config("tool.vommit.changelog", enabled=False)
    sandbox.set_config("tool.vommit.git", commit_format="")
    sandbox.commits("feat: something new")
    bump(sandbox)

    result = undo(sandbox)

    assert result.plan.changelog_path is None
    assert version_of(sandbox) == "0.1.0"


def test_undo_when_no_changelog_file_was_ever_written(sandbox):
    sandbox.set_config("tool.vommit.git", commit_format="")
    sandbox.commits("feat: something new")
    # a prerelease writes no entry, so the file never comes into being
    bump(sandbox, prerelease=True)

    result = undo(sandbox)

    assert result.plan.changelog_path is None
    assert not (sandbox.work / "CHANGELOG.md").exists()
    assert version_of(sandbox) == "0.1.0"
