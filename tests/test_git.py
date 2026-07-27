from pathlib import Path

import pytest

from src.vommit.config import CURRENT_BRANCH, DERIVE_BRANCH, GitConfig
from src.vommit.errors import VommitError
from src.vommit.git import (
    GitRepo,
    find_main_branch,
    find_main_branch_local,
    find_main_branch_upstream,
    here,
)
from src.vommit.shell import LocalRunner

from .conftest import FakeRunner, Sandbox


def repo_of(sandbox: Sandbox) -> GitRepo:
    return GitRepo(runner=LocalRunner(), root=sandbox.work)


def test_current_branch(sandbox):
    assert repo_of(sandbox).current_branch() == "main"


def test_current_branch_outside_a_repo(tmp_path):
    with pytest.raises(VommitError, match="current branch"):
        GitRepo(runner=LocalRunner(), root=tmp_path).current_branch()


def test_resolve_branch_variants(sandbox):
    repo = repo_of(sandbox)

    assert repo.resolve_branch(GitConfig.load({"branch": "release"})) == "release"
    assert repo.resolve_branch(GitConfig.load({"branch": CURRENT_BRANCH})) == "main"
    assert repo.resolve_branch(GitConfig.load({"branch": DERIVE_BRANCH})) == "main"


def test_resolve_branch_reports_a_failed_lookup():
    runner = (
        FakeRunner()
        .reply("ls-remote", returncode=1)
        .reply("init.defaultBranch", stdout="")
    )
    repo = GitRepo(runner=runner, root=Path("/nowhere"))

    with pytest.raises(VommitError, match="could not be derived"):
        repo.resolve_branch(GitConfig.load({"branch": DERIVE_BRANCH}))


def test_main_branch_lookup_helpers(sandbox, monkeypatch):
    monkeypatch.chdir(sandbox.work)

    assert find_main_branch_upstream("origin") == "main"
    assert find_main_branch("origin") == "main"
    assert here().current_branch() == "main"


def test_main_branch_local_falls_back_to_git_config(sandbox):
    sandbox.git("config", "init.defaultBranch", "trunk")
    assert repo_of(sandbox).main_branch_local() == "trunk"
    assert repo_of(sandbox).main_branch("origin", remote=False) == "trunk"


def test_find_main_branch_local_module_helper(sandbox, monkeypatch):
    sandbox.git("config", "init.defaultBranch", "trunk")
    monkeypatch.chdir(sandbox.work)
    assert find_main_branch_local() == "trunk"


def test_ensure_branch_accepts_the_release_branch(sandbox):
    messages: list[str] = []
    assert repo_of(sandbox).ensure_branch(GitConfig.load({}), messages.append) == "main"
    assert messages == []


def test_ensure_branch_errors_on_the_wrong_branch(sandbox):
    sandbox.git("checkout", "-b", "feature/x")

    with pytest.raises(VommitError, match="expected 'main'"):
        repo_of(sandbox).ensure_branch(GitConfig.load({"branch": "main"}), print)


def test_ensure_branch_can_warn_instead(sandbox):
    sandbox.git("checkout", "-b", "feature/x")
    messages: list[str] = []

    branch = repo_of(sandbox).ensure_branch(
        GitConfig.load({"branch": "main", "on_wrong_branch": "warn"}), messages.append
    )

    assert branch == "feature/x"
    assert "expected 'main'" in messages[0]


def test_ensure_branch_can_switch(sandbox):
    sandbox.git("checkout", "-b", "feature/x")
    messages: list[str] = []

    branch = repo_of(sandbox).ensure_branch(
        GitConfig.load({"branch": "main", "on_wrong_branch": "switch"}), messages.append
    )

    assert branch == "main"
    assert repo_of(sandbox).current_branch() == "main"
    assert "Switched" in messages[0]


def test_ensure_branch_reports_a_failed_switch(sandbox):
    sandbox.git("checkout", "-b", "feature/x")

    with pytest.raises(VommitError, match="switch to branch 'nope'"):
        repo_of(sandbox).ensure_branch(
            GitConfig.load({"branch": "nope", "on_wrong_branch": "switch"}), print
        )


def test_ensure_up_to_date_passes_when_in_sync(sandbox):
    repo_of(sandbox).ensure_up_to_date(GitConfig.load({}), "main", print)


def test_ensure_up_to_date_refuses_when_behind(sandbox):
    sandbox.push_upstream("feat: upstream work")

    with pytest.raises(VommitError, match="1 commit"):
        repo_of(sandbox).ensure_up_to_date(GitConfig.load({}), "main", print)


def test_ensure_up_to_date_says_so_when_there_is_no_upstream(sandbox):
    # regression: an unresolvable upstream ref used to make the check pass
    # silently, which quietly voided the whole guarantee.
    messages: list[str] = []
    sandbox.push_upstream("feat: upstream work")

    repo_of(sandbox).ensure_up_to_date(
        GitConfig.load({}), "not-pushed", messages.append
    )

    assert "No upstream branch 'origin/not-pushed'" in messages[0]


def test_ensure_up_to_date_reports_a_failed_fetch(sandbox):
    sandbox.git("remote", "set-url", "origin", str(sandbox.root / "missing.git"))

    with pytest.raises(VommitError, match="fetch from 'origin'"):
        repo_of(sandbox).ensure_up_to_date(GitConfig.load({}), "main", print)


def test_last_tag_prefers_the_configured_glob(sandbox):
    sandbox.git("tag", "nightly-1")
    sandbox.commits("feat: work")
    sandbox.git("tag", "v1.0.0")
    sandbox.commits("feat: more")

    assert repo_of(sandbox).last_tag("v*") == "v1.0.0"


def test_last_tag_falls_back_to_any_tag_when_the_glob_misses(sandbox):
    sandbox.git("tag", "nightly-1")

    assert repo_of(sandbox).last_tag("v*") == "nightly-1"


def test_last_tag_without_a_glob(sandbox):
    sandbox.git("tag", "nightly-1")

    assert repo_of(sandbox).last_tag(None) == "nightly-1"


def test_last_tag_when_there_are_none(sandbox):
    assert repo_of(sandbox).last_tag("v*") is None
    assert repo_of(sandbox).last_tag(None) is None


def test_commit_messages_since(sandbox):
    sandbox.git("tag", "v0.1.0")
    sandbox.commits("feat(api): add endpoint", "fix: correct a bug")

    assert repo_of(sandbox).commit_messages_since("v0.1.0") == [
        "fix: correct a bug",
        "feat(api): add endpoint",
    ]


def test_commit_messages_since_the_beginning(sandbox):
    assert repo_of(sandbox).commit_messages_since(None) == ["chore: init"]


def test_commit_messages_skips_merges(sandbox):
    sandbox.git("tag", "v0.1.0")
    sandbox.git("checkout", "-b", "side")
    sandbox.commits("feat: side work")
    sandbox.git("checkout", "main")
    sandbox.git("merge", "--no-ff", "-m", "Merge branch 'side'", "side")

    assert repo_of(sandbox).commit_messages_since("v0.1.0") == ["feat: side work"]


def test_commit_messages_in_an_empty_repo(tmp_path):
    (tmp_path / ".git").mkdir(parents=True)
    LocalRunner().run(f"git init {tmp_path}")

    assert (
        GitRepo(runner=LocalRunner(), root=tmp_path).commit_messages_since(None) == []
    )


def test_dirty_paths(sandbox):
    sandbox.write("pyproject.toml", "changed\n")
    sandbox.write("untracked.txt", "new\n")

    dirty = repo_of(sandbox).dirty_paths(["pyproject.toml", "README.md", "uv.lock"])

    assert dirty == ["pyproject.toml"]


def test_add_commit_and_tag(sandbox):
    sandbox.write("CHANGELOG.md", "# Changelog\n")
    repo = repo_of(sandbox)

    repo.add(["CHANGELOG.md"])
    repo.commit("1.0.0")
    repo.tag("v1.0.0")

    assert sandbox.log(1) == ["1.0.0"]
    assert sandbox.tags() == ["v1.0.0"]
    assert sandbox.status() == []


def test_add_with_nothing_to_stage_is_a_no_op(sandbox):
    runner = FakeRunner()
    GitRepo(runner=runner, root=sandbox.work).add([])

    assert runner.calls == []


def test_commit_failure_is_reported(sandbox):
    # regression: an unchecked commit used to be followed by a tag anyway,
    # leaving the tag on the previous commit.
    hook = sandbox.work / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho nope >&2\nexit 1\n")
    hook.chmod(0o755)
    sandbox.write("CHANGELOG.md", "# Changelog\n")

    repo = repo_of(sandbox)
    repo.add(["CHANGELOG.md"])

    with pytest.raises(VommitError, match="release commit"):
        repo.commit("1.0.0")


def test_tag_refuses_to_overwrite(sandbox):
    repo = repo_of(sandbox)
    repo.tag("v1.0.0")

    with pytest.raises(VommitError, match="already exists"):
        repo.tag("v1.0.0")


def test_tag_failure_is_reported(sandbox):
    with pytest.raises(VommitError, match="create tag"):
        repo_of(sandbox).tag("not a valid tag name")


def test_add_failure_is_reported(sandbox):
    with pytest.raises(VommitError, match="stage the release changes"):
        repo_of(sandbox).add(["does-not-exist.txt"])
