import datetime as dt

import pytest

from src.vommit.bump import BumpRequest
from src.vommit.config import Config
from src.vommit.errors import VommitError
from src.vommit.release import ReleaseRequest, run_release
from src.vommit.shell import LocalRunner

from .conftest import FakeRunner, Sandbox

TODAY = dt.date(2023, 4, 10)
TOKEN = "pypi-test-token"

# every step appends its name, so one file records the order of the whole run
LEDGER = "steps.txt"


def with_pipeline(sandbox: Sandbox, **overrides: str) -> None:
    """
    Configure commands that record themselves instead of touching a real index.
    """
    commands = {
        "clean": f"echo clean >> {LEDGER}",
        "build": f"echo build >> {LEDGER}",
        "publish": f"echo publish >> {LEDGER}",
    } | overrides
    sandbox.set_config("tool.vommit.commands", **commands)


def with_pypi(sandbox: Sandbox, **values) -> None:
    sandbox.set_config("tool.vommit.pypi", enabled=True, **values)


def recorder(sandbox: Sandbox, name: str, body: str) -> str:
    """
    A script that records something, and the command that runs it.

    Anything with a `$` in it has to live in a file rather than in the config:
    configuraptor expands environment variables in settings as it loads them,
    so a `$VAR` written in `pyproject.toml` never survives to reach the shell.
    """
    path = sandbox.write(name, f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    sandbox.commit(f"chore: add {name}")
    return f"./{name}"


def ledger(sandbox: Sandbox) -> list[str]:
    path = sandbox.work / LEDGER
    return path.read_text().split() if path.exists() else []


def release(
    sandbox: Sandbox,
    no_bump: bool = False,
    notify=None,
    confirm=None,
    confirm_version=None,
    authenticate=None,
    **bump_kwargs,
):
    return run_release(
        config=Config.from_pyproject(sandbox.work),
        runner=LocalRunner(),
        root=sandbox.work,
        request=ReleaseRequest(bump=BumpRequest(**bump_kwargs), no_bump=no_bump),
        notify=notify or (lambda _: None),
        today=TODAY,
        authenticate=authenticate or (lambda: TOKEN),
        **({"confirm": confirm} if confirm else {}),
        **({"confirm_version": confirm_version} if confirm_version else {}),
    )


def remote_tags(sandbox: Sandbox) -> list[str]:
    listing = sandbox.git("ls-remote", "--tags", "origin").out
    return [line.rsplit("/", 1)[-1] for line in listing.splitlines() if line]


# --- request validation ------------------------------------------------------


def test_no_bump_rejects_a_level():
    with pytest.raises(VommitError, match="drop --minor"):
        ReleaseRequest(bump=BumpRequest(level="minor"), no_bump=True)


def test_no_bump_rejects_an_explicit_version():
    with pytest.raises(VommitError, match="drop --version"):
        ReleaseRequest(bump=BumpRequest(version="2.0.0"), no_bump=True)


def test_no_bump_rejects_prerelease():
    with pytest.raises(VommitError, match="drop --prerelease"):
        ReleaseRequest(bump=BumpRequest(prerelease=True), no_bump=True)


def test_a_plain_request_is_accepted():
    request = ReleaseRequest(bump=BumpRequest(level="minor"))

    assert request.noop is False
    assert request.no_bump is False


# --- the happy path ----------------------------------------------------------


def test_release_runs_the_pipeline_in_order(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    result = release(sandbox)

    assert result.version == "0.2.0"
    assert ledger(sandbox) == ["clean", "build", "publish"]
    assert result.pushed is True
    assert result.published is True


def test_the_push_happens_between_building_and_publishing(sandbox):
    # the ordering the whole module exists for: a build failure is still
    # undoable, and PyPI never gets a version the remote does not have
    with_pipeline(
        sandbox,
        publish=f"git -C {sandbox.work} ls-remote --tags origin >> {LEDGER}",
    )
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    release(sandbox)

    assert "refs/tags/v0.2.0" in " ".join(ledger(sandbox))


def test_release_pushes_the_commit_and_the_tag(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    release(sandbox)

    assert remote_tags(sandbox) == ["v0.2.0"]
    assert sandbox.git("log", "-1", "--format=%s", "origin/main").out == "0.2.0"


def test_only_the_release_tag_is_pushed(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.git("tag", "scratch")
    sandbox.commits("feat: something releasable")

    release(sandbox)

    assert "scratch" not in remote_tags(sandbox)


def test_the_publish_step_gets_the_token(sandbox):
    script = recorder(sandbox, "show-token.sh", f'echo "$UV_PUBLISH_TOKEN" >> {LEDGER}')
    with_pipeline(sandbox, publish=script)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    release(sandbox)

    assert TOKEN in ledger(sandbox)


def test_the_build_step_never_sees_the_token(sandbox):
    script = recorder(sandbox, "show-token.sh", f'echo "b[$UV_PUBLISH_TOKEN]" >> {LEDGER}')
    with_pipeline(sandbox, build=script)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    release(sandbox)

    assert "b[]" in ledger(sandbox)


def test_the_token_is_read_before_anything_is_written(sandbox):
    # a missing token must not surface after the commit and tag already exist
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    def refuse() -> str:
        raise VommitError("No PyPI token stored")

    with pytest.raises(VommitError, match="No PyPI token stored"):
        release(sandbox, authenticate=refuse)

    assert sandbox.tags() == []
    assert ledger(sandbox) == []


def test_an_empty_command_skips_that_step(sandbox):
    with_pipeline(sandbox, clean="")
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    result = release(sandbox)

    assert [step.name for step in result.steps] == ["build", "publish"]
    assert ledger(sandbox) == ["build", "publish"]


def test_an_empty_publish_command_asks_for_no_token(sandbox):
    with_pipeline(sandbox, publish="")
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    result = release(
        sandbox,
        authenticate=lambda: pytest.fail("nothing publishes, so nothing needs a token"),
    )

    assert result.published is False
    assert ledger(sandbox) == ["clean", "build"]


def test_post_publish_runs_last(sandbox):
    with_pipeline(sandbox, post_publish=f"echo tidy >> {LEDGER}")
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    release(sandbox)

    assert ledger(sandbox) == ["clean", "build", "publish", "tidy"]


def test_post_publish_is_skipped_without_a_publish(sandbox):
    # it is named after the step it follows; with nothing published there is
    # nothing for it to follow
    with_pipeline(sandbox, post_publish=f"echo tidy >> {LEDGER}")
    sandbox.set_config("tool.vommit.pypi", enabled=False)
    sandbox.commits("feat: something releasable")

    release(sandbox)

    assert ledger(sandbox) == ["clean", "build"]


def test_post_publish_does_not_get_the_token(sandbox):
    script = recorder(sandbox, "show-token.sh", f'echo "p[$UV_PUBLISH_TOKEN]" >> {LEDGER}')
    with_pipeline(sandbox, post_publish=script)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    release(sandbox)

    assert "p[]" in ledger(sandbox)


def test_a_failing_post_publish_says_the_release_went_out(sandbox):
    with_pipeline(sandbox, post_publish="exit 1")
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    with pytest.raises(VommitError, match="is published; only the follow-up"):
        release(sandbox)


def test_no_bump_allows_allow_dirty():
    # it does not choose a version, so it does not conflict with --no-bump
    request = ReleaseRequest(bump=BumpRequest(allow_dirty=True), no_bump=True)

    assert request.no_bump is True


# --- pypi disabled, no commands ----------------------------------------------


def test_publishing_is_skipped_when_pypi_is_disabled(sandbox):
    with_pipeline(sandbox)
    sandbox.set_config("tool.vommit.pypi", enabled=False)
    sandbox.commits("feat: something releasable")

    result = release(sandbox)

    assert ledger(sandbox) == ["clean", "build"]
    assert result.published is False
    assert result.pushed is True


def test_a_project_without_commands_falls_back_to_the_defaults(sandbox):
    # `clean` has to survive a project that has never built, or no first
    # release would ever get past it
    sandbox.set_config("tool.vommit.pypi", enabled=False)
    sandbox.set_config("tool.vommit.commands", build="true")
    sandbox.commits("feat: something releasable")

    result = release(sandbox)

    assert [step.name for step in result.steps] == ["clean", "build"]
    assert result.pushed is True
    assert remote_tags(sandbox) == ["v0.2.0"]


def test_a_config_without_any_commands_only_bumps_and_pushes(sandbox):
    sandbox.commits("feat: something releasable")
    config = Config.from_pyproject(sandbox.work)
    config.commands = None

    result = run_release(
        config=config,
        runner=LocalRunner(),
        root=sandbox.work,
        request=ReleaseRequest(bump=BumpRequest()),
        today=TODAY,
        authenticate=lambda: TOKEN,
    )

    assert result.steps == ()
    assert result.published is False
    assert remote_tags(sandbox) == ["v0.2.0"]


def test_nothing_is_pushed_when_git_is_disabled(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.set_config("tool.vommit.git", enabled=False)
    sandbox.commits("feat: something releasable")

    result = release(sandbox)

    assert result.pushed is False
    assert remote_tags(sandbox) == []
    assert ledger(sandbox) == ["clean", "build", "publish"]


# --- noop --------------------------------------------------------------------


def test_noop_stops_after_the_preview(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    result = release(sandbox, noop=True)

    assert result.noop is True
    assert result.version == "0.2.0"
    assert [step.name for step in result.steps] == ["clean", "build", "publish"]
    assert ledger(sandbox) == []
    assert sandbox.tags() == []
    assert remote_tags(sandbox) == []


def test_noop_does_not_ask_for_a_token(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    result = release(
        sandbox,
        noop=True,
        authenticate=lambda: pytest.fail("a dry run has no reason to authenticate"),
    )

    assert result.noop is True


def test_noop_reports_the_commands_it_would_run(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    result = release(sandbox, noop=True)

    assert result.steps[0].command == f"echo clean >> {LEDGER}"


# --- confirmation ------------------------------------------------------------


def test_a_declined_bump_releases_nothing(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    result = release(sandbox, confirm=lambda _: False)

    assert result.cancelled is True
    assert ledger(sandbox) == []
    assert sandbox.tags() == []


# --- nothing to bump ---------------------------------------------------------


def test_nothing_to_bump_offers_the_current_version(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("chore: nothing worth releasing")

    asked: list[str] = []

    result = release(
        sandbox,
        confirm_version=lambda version: asked.append(version) or True,
    )

    assert asked == ["0.1.0"]
    assert result.version == "0.1.0"
    assert result.bump is None
    assert ledger(sandbox) == ["clean", "build", "publish"]


def test_nothing_to_bump_goes_ahead_when_nobody_is_asked(sandbox):
    # the library default: a caller with no way to ask does not get blocked
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("chore: nothing worth releasing")

    result = release(sandbox)

    assert result.version == "0.1.0"
    assert ledger(sandbox) == ["clean", "build", "publish"]


def test_declining_that_offer_releases_nothing(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("chore: nothing worth releasing")

    result = release(sandbox, confirm_version=lambda _: False)

    assert result is None
    assert ledger(sandbox) == []


# --- --no-bump ---------------------------------------------------------------


def test_no_bump_publishes_what_is_already_there(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    result = release(sandbox, no_bump=True)

    assert result.version == "0.1.0"
    assert result.bump is None
    assert ledger(sandbox) == ["clean", "build", "publish"]


def test_no_bump_pushes_an_existing_tag_the_remote_is_missing(sandbox):
    # the resume path: the tag was made locally, the publish failed, retry
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.git("tag", "v0.1.0")

    release(sandbox, no_bump=True)

    assert remote_tags(sandbox) == ["v0.1.0"]


def test_no_bump_without_a_tag_pushes_only_the_branch(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)

    result = release(sandbox, no_bump=True)

    assert result.pushed is True
    assert remote_tags(sandbox) == []


def test_no_bump_still_refuses_a_stale_branch(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.push_upstream("feat: landed elsewhere")

    with pytest.raises(VommitError, match="behind"):
        release(sandbox, no_bump=True)

    assert ledger(sandbox) == []


def test_no_bump_needs_a_version_to_publish(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.write("pyproject.toml", "[project]\nname = 'sandbox'\n")
    sandbox.commit("chore: drop the version")

    with pytest.raises(VommitError, match="nothing to release"):
        release(sandbox, no_bump=True)


# --- the artifact has to match the commit -------------------------------------
#
# Two ways the tree that gets built can differ from the history that gets
# pushed. Both end with the index holding a version that no commit contains,
# and PyPI refusing the reupload that would put it right.


def test_a_modified_source_file_stops_the_release(sandbox):
    # `bump` only checks the files it writes, so this used to build and publish
    # a change that the release commit never contained
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")
    (sandbox.work / "src/sandbox/__init__.py").write_text("SECRET = 1\n")

    with pytest.raises(VommitError, match="src/sandbox/__init__.py"):
        release(sandbox)

    assert ledger(sandbox) == [], "nothing should have been built"


def test_an_untracked_source_file_stops_the_release(sandbox):
    # untracked counts too: the build picks it up, the commit does not
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")
    (sandbox.work / "src/sandbox/extra.py").write_text("x = 1\n")

    with pytest.raises(VommitError, match="src/sandbox/extra.py"):
        release(sandbox)


def test_a_dirty_tree_stops_the_release_before_the_token_is_asked_for(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")
    (sandbox.work / "src/sandbox/__init__.py").write_text("SECRET = 1\n")

    def refuse():  # pragma: no cover
        raise AssertionError("a doomed release should not ask for a credential")

    with pytest.raises(VommitError, match="Commit or stash"):
        release(sandbox, authenticate=refuse)


def test_allow_dirty_publishes_the_tree_as_it_stands(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")
    (sandbox.work / "src/sandbox/__init__.py").write_text("SECRET = 1\n")

    result = release(sandbox, allow_dirty=True)

    assert result.published is True
    assert ledger(sandbox) == ["clean", "build", "publish"]


def test_no_bump_checks_the_tree_too(sandbox):
    # the retry path never calls `run_bump`, so it used to skip every check
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")
    (sandbox.work / "src/sandbox/__init__.py").write_text("SECRET = 1\n")

    with pytest.raises(VommitError, match="src/sandbox/__init__.py"):
        release(sandbox, no_bump=True)


def test_a_dirty_tree_is_ignored_when_git_is_off(sandbox):
    # nothing is pushed, so there is no remote for the artifact to disagree with
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.set_config("tool.vommit.git", enabled=False)
    sandbox.commits("feat: something releasable")
    (sandbox.work / "src/sandbox/__init__.py").write_text("SECRET = 1\n")

    assert release(sandbox).published is True


def test_a_release_without_a_commit_format_is_refused(sandbox):
    # regression: `commit_format = ""` means run_bump writes and stages the
    # bump but never commits it, then tags the commit *before* the bump. the
    # push sent an unchanged branch and a tag on the wrong commit, and publish
    # uploaded a version that no commit anywhere contained
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.set_config("tool.vommit.git", commit_format="")
    sandbox.commits("feat: something releasable")

    with pytest.raises(VommitError, match="`git.commit_format` is empty"):
        release(sandbox)

    assert ledger(sandbox) == [], "nothing should have been built"
    assert remote_tags(sandbox) == [], "no tag should have reached the remote"
    assert sandbox.tags() == [], "and none should have been created locally"


def test_that_refusal_names_both_ways_out(sandbox):
    with_pipeline(sandbox)
    sandbox.set_config("tool.vommit.git", commit_format="")

    with pytest.raises(VommitError, match="git.enabled = false"):
        release(sandbox)


def test_no_bump_is_refused_without_a_commit_format_too(sandbox):
    # the original release was never committed either, so retrying it cannot
    # put the version onto the remote
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.set_config("tool.vommit.git", commit_format="")

    with pytest.raises(VommitError, match="`git.commit_format` is empty"):
        release(sandbox, no_bump=True)


def test_an_empty_commit_format_is_fine_without_git(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.set_config("tool.vommit.git", enabled=False, commit_format="")
    sandbox.commits("feat: something releasable")

    assert release(sandbox).published is True


# --- failures ----------------------------------------------------------------


def test_a_build_failure_says_the_release_can_still_be_undone(sandbox):
    with_pipeline(sandbox, build="exit 3")
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    with pytest.raises(VommitError) as caught:
        release(sandbox)

    assert "`build` failed" in str(caught.value)
    assert "bump --undo" in str(caught.value)
    # nothing left the machine, so the claim in that message holds
    assert remote_tags(sandbox) == []


def test_a_publish_failure_points_at_the_resume_flag(sandbox):
    with_pipeline(sandbox, publish="exit 1")
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    with pytest.raises(VommitError) as caught:
        release(sandbox)

    assert "release --no-bump" in str(caught.value)
    assert remote_tags(sandbox) == ["v0.2.0"]


def test_a_403_blames_the_token(sandbox):
    with_pipeline(sandbox, publish="echo 'Upload failed: 403 Forbidden'; exit 1")
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    with pytest.raises(VommitError, match="vommit authenticate"):
        release(sandbox)


def test_an_ordinary_failure_does_not_blame_the_token(sandbox):
    with_pipeline(sandbox, publish="echo 'Upload failed: 400 Bad Request'; exit 1")
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    with pytest.raises(VommitError) as caught:
        release(sandbox)

    assert "vommit authenticate" not in str(caught.value)


def test_a_failure_without_a_push_says_so(sandbox):
    with_pipeline(sandbox, publish="exit 1")
    with_pypi(sandbox)
    sandbox.set_config("tool.vommit.git", enabled=False)
    sandbox.commits("feat: something releasable")

    with pytest.raises(VommitError, match="built but not published"):
        release(sandbox)


def test_a_no_bump_failure_does_not_promise_an_undo(sandbox):
    with_pipeline(sandbox, build="exit 3")
    with_pypi(sandbox)

    with pytest.raises(VommitError) as caught:
        release(sandbox, no_bump=True)

    assert "Nothing was pushed or published." in str(caught.value)
    assert "bump --undo" not in str(caught.value)


def test_a_failed_push_stops_before_publishing(sandbox):
    with_pipeline(sandbox)
    with_pypi(sandbox)
    # a pushurl only git-push uses, so fetching and the freshness check still work
    sandbox.git("remote", "set-url", "--push", "origin", "/nowhere.git")
    sandbox.commits("feat: something releasable")

    with pytest.raises(VommitError, match="push 'main' to 'origin'"):
        release(sandbox)

    assert ledger(sandbox) == ["clean", "build"]


# --- plumbing ----------------------------------------------------------------


def test_commands_run_from_the_project_root(sandbox):
    with_pipeline(sandbox, build=f"pwd >> {LEDGER}")
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    release(sandbox)

    assert ledger(sandbox) == ["clean", str(sandbox.work), "publish"]


def test_each_step_is_reported_while_it_runs(sandbox):
    from contextlib import contextmanager

    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    seen: list[str] = []

    @contextmanager
    def report(name: str):
        seen.append(name)
        yield

    run_release(
        config=Config.from_pyproject(sandbox.work),
        runner=LocalRunner(),
        root=sandbox.work,
        request=ReleaseRequest(bump=BumpRequest()),
        today=TODAY,
        authenticate=lambda: TOKEN,
        report_step=report,
    )

    assert seen == ["clean", "build", "publish"]


def test_the_token_never_reaches_the_recorded_command(sandbox):
    runner = FakeRunner()
    runner.reply("rev-parse --abbrev-ref HEAD", stdout="main")

    with_pipeline(sandbox)
    with_pypi(sandbox)
    sandbox.commits("feat: something releasable")

    run_release(
        config=Config.from_pyproject(sandbox.work),
        runner=runner,
        root=sandbox.work,
        request=ReleaseRequest(bump=BumpRequest(), no_bump=True),
        today=TODAY,
        authenticate=lambda: TOKEN,
    )

    assert not any(TOKEN in call for call in runner.calls)
    assert runner.env_for("echo publish") == {"UV_PUBLISH_TOKEN": TOKEN}
    assert runner.env_for("echo build") is None
