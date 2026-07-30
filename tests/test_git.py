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
    resolve_author,
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


def test_commit_can_carry_a_configured_author(sandbox):
    """
    `--author` names who wrote it; the committer stays whoever ran the release,
    which is what git records and what a signature would cover.
    """
    sandbox.write("CHANGELOG.md", "# Changelog\n")
    repo = repo_of(sandbox)

    repo.add(["CHANGELOG.md"])
    repo.commit("1.0.0", "Release Bot <bot@example.com>")

    assert sandbox.git("log", "-1", "--format=%an <%ae>").out == (
        "Release Bot <bot@example.com>"
    )
    assert (
        sandbox.git("log", "-1", "--format=%cn <%ce>").out == "test <test@example.com>"
    )


def test_commit_without_an_author_stays_with_the_committer(sandbox):
    sandbox.write("CHANGELOG.md", "# Changelog\n")
    repo = repo_of(sandbox)

    repo.add(["CHANGELOG.md"])
    repo.commit("1.0.0")

    assert (
        sandbox.git("log", "-1", "--format=%an <%ae>").out == "test <test@example.com>"
    )
    assert "--author" not in sandbox.git("log", "-1", "--format=%s").out


@pytest.mark.parametrize(
    "configured,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("Bot <bot@example.com>", "Bot <bot@example.com>"),
        ("  Bot <bot@example.com>  ", "Bot <bot@example.com>"),
        ("Bot <>", "Bot <>"),
    ],
)
def test_resolve_author_accepts_what_git_accepts(configured, expected):
    assert resolve_author(configured) == expected


@pytest.mark.parametrize("configured", ["Bot", "bot@example.com", "Bot <a> <b>", "<a>"])
def test_resolve_author_refuses_what_git_would_reject(configured):
    """
    Refused before the bump writes anything: git only rejects it at commit time,
    by which point the version is changed and staged.
    """
    with pytest.raises(VommitError, match="git takes 'Name <email>'"):
        resolve_author(configured)


def test_add_with_nothing_to_stage_is_a_no_op(sandbox):
    runner = FakeRunner()
    GitRepo(runner=runner, root=sandbox.work).add([])

    assert runner.calls == []


def test_ignores_an_untracked_ignored_path(sandbox):
    sandbox.write(".gitignore", "uv.lock\n")

    assert repo_of(sandbox).ignores("uv.lock") is True


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


def _versions(git: GitConfig):
    return lambda tag: git.parse_tag(tag)


def test_last_stable_tag_skips_prereleases(sandbox):
    git = GitConfig.load({})
    sandbox.git("tag", "v1.0.0")
    sandbox.commits("feat: work")
    sandbox.git("tag", "v1.1.0rc1")
    sandbox.commits("fix: work")
    sandbox.git("tag", "v1.1.0rc2")

    repo = repo_of(sandbox)
    assert repo.last_tag("v*") == "v1.1.0rc2"
    assert repo.last_stable_tag("v*", _versions(git)) == "v1.0.0"


def test_last_stable_tag_ignores_tags_of_another_shape(sandbox):
    git = GitConfig.load({})
    sandbox.git("tag", "v1.0.0")
    sandbox.commits("feat: work")
    sandbox.git("tag", "nightly-2")

    assert repo_of(sandbox).last_stable_tag(None, _versions(git)) == "v1.0.0"


def test_last_stable_tag_when_every_tag_is_a_prerelease(sandbox):
    git = GitConfig.load({})
    sandbox.git("tag", "v0.1.0rc1")

    assert repo_of(sandbox).last_stable_tag("v*", _versions(git)) is None


def test_last_stable_tag_when_there_are_no_tags(sandbox):
    assert repo_of(sandbox).last_stable_tag("v*", lambda tag: tag) is None


def test_tags_reachable_excludes_other_branches(sandbox):
    sandbox.git("tag", "v1.0.0")
    sandbox.git("checkout", "-b", "side")
    sandbox.commits("feat: elsewhere")
    sandbox.git("tag", "v2.0.0")
    sandbox.git("checkout", "main")

    assert repo_of(sandbox).tags_reachable("v*") == ["v1.0.0"]


def test_remote_branches_containing_an_unknown_ref(sandbox):
    assert repo_of(sandbox).remote_branches_containing("nope") == []


def test_remote_branches_containing_names_the_pushed_branch(sandbox):
    repo = repo_of(sandbox)
    assert repo.remote_branches_containing("HEAD") == ["origin/main"]

    sandbox.commits("feat: unpushed")
    assert repo.remote_branches_containing("HEAD") == []


def test_remote_has_tag(sandbox):
    repo = repo_of(sandbox)
    sandbox.git("tag", "v1.0.0")

    assert repo.remote_has_tag("origin", "v1.0.0") is False

    sandbox.git("push", "origin", "v1.0.0")
    assert repo.remote_has_tag("origin", "v1.0.0") is True


def test_tag_commit_and_head_commit(sandbox):
    repo = repo_of(sandbox)
    sandbox.git("tag", "v1.0.0")

    assert repo.tag_commit("v1.0.0") == repo.head_commit()
    assert repo.tag_commit("v9.9.9") is None


def test_head_subject(sandbox):
    sandbox.commits("feat: the latest thing")
    assert repo_of(sandbox).head_subject() == "feat: the latest thing"


def test_delete_tag_and_reset_hard(sandbox):
    repo = repo_of(sandbox)
    sandbox.commits("feat: something")
    sandbox.git("tag", "v1.0.0")
    before = sandbox.git("rev-parse", "HEAD~1").out

    repo.delete_tag("v1.0.0")
    repo.reset_hard("HEAD~1")

    assert sandbox.tags() == []
    assert sandbox.git("rev-parse", "HEAD").out == before


def test_delete_a_tag_that_is_not_there(sandbox):
    with pytest.raises(VommitError, match="delete tag"):
        repo_of(sandbox).delete_tag("v9.9.9")


def test_file_at(sandbox):
    repo = repo_of(sandbox)

    assert "sandbox" in (repo.file_at("HEAD", "README.md") or "")
    assert repo.file_at("HEAD", "not-there.md") is None


def test_unstage_leaves_the_working_tree_alone(sandbox):
    repo = repo_of(sandbox)
    sandbox.write("README.md", "changed\n")
    sandbox.git("add", "README.md")
    assert sandbox.status() == ["M  README.md"]

    repo.unstage(["README.md"])

    # unstaged but still modified (Sandbox.status strips the leading column)
    assert sandbox.status() == ["M README.md"]
    assert (sandbox.work / "README.md").read_text() == "changed\n"


def test_unstage_without_paths_runs_nothing(sandbox):
    runner = FakeRunner()
    GitRepo(runner=runner, root=sandbox.work).unstage([])
    assert runner.calls == []


def test_push_sends_the_branch(sandbox):
    repo = repo_of(sandbox)
    sandbox.commit("feat: something", empty=True)

    repo.push("origin", "main")

    assert sandbox.git("log", "-1", "--format=%s", "origin/main").out == (
        "feat: something"
    )


def test_push_reports_a_failure(sandbox):
    with pytest.raises(VommitError, match="push 'nope' to 'origin'"):
        repo_of(sandbox).push("origin", "nope")


def test_push_tag_sends_a_lightweight_tag(sandbox):
    # --follow-tags would silently skip this one; pushing the ref does not
    repo = repo_of(sandbox)
    repo.tag("v1.0.0")

    repo.push_tag("origin", "v1.0.0")

    assert repo.remote_has_tag("origin", "v1.0.0") is True


def test_push_tag_sends_only_the_named_tag(sandbox):
    repo = repo_of(sandbox)
    repo.tag("v1.0.0")
    repo.tag("scratch")

    repo.push_tag("origin", "v1.0.0")

    assert repo.remote_has_tag("origin", "scratch") is False


def test_push_tag_reports_a_failure(sandbox):
    with pytest.raises(VommitError, match="push tag 'v9.9.9'"):
        repo_of(sandbox).push_tag("origin", "v9.9.9")


def bare(tmp_path: Path, branch: str = "main") -> GitRepo:
    """
    A repository with no commit yet: what `uv init` leaves behind.
    """
    runner = LocalRunner()
    runner.run(f"git init --initial-branch={branch} {tmp_path}")
    return GitRepo(runner=runner, root=tmp_path)


def test_current_branch_before_the_first_commit(tmp_path):
    """
    Regression: `rev-parse --abbrev-ref HEAD` fails outright on an unborn
    branch, which made every command in a freshly created project die with
    git's "ambiguous argument 'HEAD'".
    """
    assert bare(tmp_path, "trunk").current_branch() == "trunk"


def test_current_branch_on_a_detached_head(sandbox):
    """
    `symbolic-ref` cannot answer here, and this is what CI checkouts look like.
    """
    sandbox.git("checkout", "--detach")

    assert repo_of(sandbox).current_branch() == "HEAD"


def test_detect_release_branch_prefers_the_remote(sandbox):
    sandbox.git("checkout", "-b", "feature/x")

    # the checked-out branch is not the answer: someone running `setup` from a
    # feature branch means to release from the project's main branch
    assert repo_of(sandbox).detect_release_branch() == "main"


def test_detect_release_branch_uses_the_checkout_without_a_remote(tmp_path):
    assert bare(tmp_path, "trunk").detect_release_branch() == "trunk"


def test_detect_release_branch_falls_back_to_the_git_default(tmp_path):
    runner = (
        FakeRunner()
        .reply("ls-remote", returncode=1)
        .reply("symbolic-ref", returncode=128)
        .reply("init.defaultBranch", stdout="master")
    )

    assert GitRepo(runner=runner, root=tmp_path).detect_release_branch() == "master"


def test_detect_release_branch_gives_up_cleanly(tmp_path):
    runner = (
        FakeRunner()
        .reply("ls-remote", returncode=1)
        .reply("symbolic-ref", returncode=128)
        .reply("init.defaultBranch", stdout="")
    )

    assert GitRepo(runner=runner, root=tmp_path).detect_release_branch() is None


def test_is_repo_root(tmp_path):
    assert bare(tmp_path).is_repo_root() is True


def test_is_repo_root_is_false_for_a_directory_inside_one(tmp_path):
    """
    `uv init` skips `git init` inside a repository, and the toplevel that
    answers then belongs to somebody else.
    """
    repo = bare(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()

    assert GitRepo(runner=repo.runner, root=nested).is_repo_root() is False


def test_is_repo_root_is_false_outside_a_repo(tmp_path):
    assert GitRepo(runner=LocalRunner(), root=tmp_path).is_repo_root() is False


def test_rename_branch_works_before_the_first_commit(tmp_path):
    repo = bare(tmp_path, "master")

    repo.rename_branch("main")

    assert repo.current_branch() == "main"


def test_rename_branch_reports_a_failure(tmp_path):
    runner = FakeRunner().reply("branch -m", returncode=1, stderr="nope")

    with pytest.raises(VommitError, match="rename the branch to 'main'"):
        GitRepo(runner=runner, root=tmp_path).rename_branch("main")


def test_remotes(sandbox):
    assert repo_of(sandbox).remotes() == ["origin"]


def test_remotes_is_empty_outside_a_repo(tmp_path):
    assert GitRepo(runner=LocalRunner(), root=tmp_path).remotes() == []


def test_add_remote(tmp_path):
    repo = bare(tmp_path)

    repo.add_remote("origin", "git@example.com:me/mypkg.git")

    assert repo.remotes() == ["origin"]


def test_add_remote_reports_a_failure(sandbox):
    with pytest.raises(VommitError, match="add remote 'origin'"):
        repo_of(sandbox).add_remote("origin", "git@example.com:me/mypkg.git")


def test_push_upstream_records_the_upstream(sandbox):
    sandbox.git("checkout", "-b", "release")

    repo_of(sandbox).push_upstream("origin", "release")

    assert repo_of(sandbox).ref_exists("origin/release")


def test_push_upstream_reports_a_failure(sandbox):
    with pytest.raises(VommitError, match="push 'nope' to 'origin'"):
        repo_of(sandbox).push_upstream("origin", "nope")


def test_ensure_up_to_date_skips_a_project_without_a_remote(tmp_path):
    """
    Regression: the fetch ran before anything checked the remote existed, so a
    project with no remote yet could not bump at all.
    """
    repo = bare(tmp_path)
    messages: list[str] = []

    repo.ensure_up_to_date(GitConfig.load({}), "main", messages.append)

    assert "No remote 'origin'" in messages[0]
