import datetime as dt
import shlex
import typing as t
import dataclasses as dc
from pathlib import Path

import pytest

from src.vommit import licenses
from src.vommit.errors import VommitError
from src.vommit.git import GitRepo
from src.vommit.scaffold import (
    DEFAULT_BRANCH,
    DEFAULT_COMMIT_MESSAGE,
    FALLBACK_GITIGNORE,
    GITIGNORE,
    LICENSE_FILE,
    Defaults,
    ScaffoldRequest,
    copyright_holder,
    default_python,
    ensure_gitignore,
    ignore,
    plan_request,
    run_scaffold,
    uv_init_argv,
    write_license,
)
from src.vommit.shell import LocalRunner

from .conftest import FakeRunner

PYPROJECT = """\
[project]
name = "mypkg"
version = "0.1.0"
authors = [
    { name = "Robin", email = "robin@example.com" }
]
"""


@dc.dataclass
class ScriptedAsker:
    """
    An asker reading from prepared answers, keyed on the question.

    Keys match on the start of the question, not anywhere in it: "License" is a
    substring of "SPDX license identifier", and answering both with one key hid a
    bug here. Anything unscripted falls through to the default, which is what
    makes the "a flag pre-fills its prompt" behaviour visible in a test.
    """

    answers: dict[str, t.Any] = dc.field(default_factory=dict)
    asked: dict[str, t.Any] = dc.field(default_factory=dict)

    def _answer(self, question: str, default: t.Any) -> t.Any:
        self.asked[question] = default
        for needle, answer in self.answers.items():
            if question.lower().startswith(needle.lower()):
                return answer
        return default

    def text(self, question: str, default: str = "") -> str:
        return str(self._answer(question, default))

    def confirm(self, question: str, default: bool = True) -> bool:
        return bool(self._answer(question, default))

    def choose(self, question: str, options: list[str], default: str) -> str:
        assert default in options, f"{default!r} is not one of {options}"
        return str(self._answer(question, default))


def project(root: Path, content: str = PYPROJECT) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(content)
    return root


def uv_init_faker(root: Path, branch: str = "master") -> FakeRunner:
    """
    A runner that creates the project `uv init` would have, then answers git.
    """
    runner = FakeRunner()
    real = LocalRunner()

    def run(command: str, env: dict[str, str] | None = None) -> t.Any:
        runner.calls.append(command)
        runner.envs.append(env)
        if " init --package" in command:
            project(root)
            real.run(shlex.join(["git", "init", f"--initial-branch={branch}", str(root)]))
            real.run(shlex.join(["git", "-C", str(root), "config", "user.name", "t"]))
            real.run(shlex.join(["git", "-C", str(root), "config", "user.email", "t@t.nl"]))
            real.run(shlex.join(["git", "-C", str(root), "config", "commit.gpgsign", "false"]))
            return runner.default
        for needle, result in runner.responses.items():
            if needle in command:
                return result
        if command.startswith("git "):
            return real.run(command)
        return runner.default

    runner.run = run  # type: ignore[method-assign]
    return runner


# --- uv_init_argv ------------------------------------------------------------


def test_uv_init_argv_skips_the_python_pin_by_default(tmp_path):
    argv = uv_init_argv(ScaffoldRequest(project_name="mypkg"), tmp_path / "mypkg")
    assert "--no-pin-python" in argv
    assert argv[-1] == str(tmp_path / "mypkg")


def test_uv_init_argv_keeps_the_pin_when_asked(tmp_path):
    argv = uv_init_argv(
        ScaffoldRequest(project_name="mypkg", pin_python=True), tmp_path / "mypkg"
    )
    assert "--no-pin-python" not in argv


def test_uv_init_argv_passes_the_python_floor(tmp_path):
    argv = uv_init_argv(
        ScaffoldRequest(project_name="mypkg", python="3.13"), tmp_path / "mypkg"
    )
    assert argv[argv.index("--python") + 1] == "3.13"


def test_uv_init_argv_drops_the_description_placeholder(tmp_path):
    """
    An absent description must become `--no-description`, or uv's "Add your
    description here" ends up on PyPI.
    """
    argv = uv_init_argv(ScaffoldRequest(project_name="mypkg"), tmp_path / "mypkg")
    assert "--no-description" in argv
    assert "--description" not in argv


def test_uv_init_argv_passes_a_description(tmp_path):
    argv = uv_init_argv(
        ScaffoldRequest(project_name="mypkg", description="does things"),
        tmp_path / "mypkg",
    )
    assert argv[argv.index("--description") + 1] == "does things"
    assert "--no-description" not in argv


def test_uv_init_argv_can_stay_out_of_a_workspace(tmp_path):
    argv = uv_init_argv(
        ScaffoldRequest(project_name="mypkg", workspace=False), tmp_path / "mypkg"
    )
    assert "--no-workspace" in argv


# --- ScaffoldRequest ---------------------------------------------------------


def test_request_refuses_an_empty_project_name():
    with pytest.raises(VommitError, match="project name"):
        ScaffoldRequest(project_name="  ")


def test_request_refuses_an_empty_branch():
    with pytest.raises(VommitError, match="branch name"):
        ScaffoldRequest(project_name="mypkg", branch=" ")


def test_request_refuses_a_push_without_a_remote():
    with pytest.raises(VommitError, match="needs a remote"):
        ScaffoldRequest(project_name="mypkg", push=True)


# --- plan_request ------------------------------------------------------------


def test_defaults_asker_hands_the_request_back_unchanged():
    defaults = ScaffoldRequest(
        project_name="mypkg",
        python="3.13",
        description="does things",
        license_id="MIT",
        branch="trunk",
        remote="git@example.com:me/mypkg.git",
        commit_message="chore: start",
        push=True,
        sync=True,
    )
    assert plan_request(defaults, Defaults()) == defaults


def test_plan_request_uses_the_detected_branch_over_the_default():
    planned = plan_request(
        ScaffoldRequest(project_name="mypkg"), Defaults(), detected_branch="trunk"
    )
    assert planned.branch == "trunk"


def test_plan_request_prefers_an_explicit_branch_flag_shape():
    """
    `detected_branch=None` is how the task says "the flag already decided".
    """
    planned = plan_request(
        ScaffoldRequest(project_name="mypkg", branch="release"), Defaults()
    )
    assert planned.branch == "release"


def test_plan_request_falls_back_when_the_branch_answer_is_blank():
    planned = plan_request(
        ScaffoldRequest(project_name="mypkg"),
        ScriptedAsker({"Release branch": "   "}),
        detected_branch="trunk",
    )
    assert planned.branch == "trunk"


def test_plan_request_defaults_the_python_floor_to_the_interpreter():
    planned = plan_request(ScaffoldRequest(project_name="mypkg"), Defaults())
    assert planned.python == default_python()


def test_plan_request_asks_for_an_spdx_id_when_the_license_is_other():
    planned = plan_request(
        ScaffoldRequest(project_name="mypkg"),
        ScriptedAsker({"License": licenses.OTHER_LICENSE, "SPDX": "Apache-2.0"}),
    )
    assert planned.license_id == "Apache-2.0"


def test_plan_request_treats_a_blank_spdx_id_as_no_license():
    planned = plan_request(
        ScaffoldRequest(project_name="mypkg"),
        ScriptedAsker({"License": licenses.OTHER_LICENSE, "SPDX": "  "}),
    )
    assert planned.license_id == licenses.NO_LICENSE


def test_plan_request_offers_an_unknown_license_id_as_the_fallback():
    """
    A `--license` we carry no text for must not become the offered default, or
    the select would open on a choice that is not in the list.
    """
    asker = ScriptedAsker()
    plan_request(ScaffoldRequest(project_name="mypkg", license_id="Apache-2.0"), asker)
    assert asker.asked["License"] == licenses.DEFAULT_LICENSE


def test_plan_request_offers_a_known_license_id_as_given():
    asker = ScriptedAsker()
    plan_request(ScaffoldRequest(project_name="mypkg", license_id="ISC"), asker)
    assert asker.asked["License"] == "ISC"


def test_plan_request_skips_the_commit_message_when_the_commit_is_declined():
    planned = plan_request(
        ScaffoldRequest(project_name="mypkg", commit_message=DEFAULT_COMMIT_MESSAGE),
        ScriptedAsker({"Make an initial commit?": False}),
    )
    assert planned.commit_message is None
    assert planned.push is False


def test_plan_request_does_not_offer_a_push_without_a_remote():
    planned = plan_request(
        ScaffoldRequest(project_name="mypkg", commit_message="chore: init"),
        ScriptedAsker({"Push to": True}),
    )
    assert planned.push is False


def test_plan_request_offers_a_push_once_there_is_a_remote_and_a_commit():
    planned = plan_request(
        ScaffoldRequest(project_name="mypkg", commit_message="chore: init"),
        ScriptedAsker({"Git remote": "git@example.com:me/mypkg.git", "Push to": True}),
    )
    assert planned.push is True


def test_plan_request_can_turn_the_sync_off():
    planned = plan_request(
        ScaffoldRequest(project_name="mypkg", sync=True),
        ScriptedAsker({"Run `uv sync`": False}),
    )
    assert planned.sync is False


# --- licenses ----------------------------------------------------------------


@pytest.mark.parametrize("license_id", sorted(licenses.TEMPLATES))
def test_every_offered_license_renders_with_the_holder_and_year(license_id):
    text = licenses.render(license_id, "Robin", 2026)
    assert text
    assert "Robin" in text
    assert "2026" in text
    assert "{" not in text


def test_render_declines_a_license_it_does_not_carry():
    assert licenses.render("Apache-2.0", "Robin", 2026) is None


def test_write_license_writes_the_file_and_the_metadata(tmp_path):
    root = project(tmp_path / "mypkg")
    assert write_license(root, "MIT", 2026) is True

    text = (root / LICENSE_FILE).read_text()
    assert "MIT License" in text
    assert "Copyright (c) 2026 Robin" in text

    written = (root / "pyproject.toml").read_text()
    assert 'license = "MIT"' in written
    assert 'license-files = ["LICENSE"]' in written


def test_write_license_records_an_id_it_has_no_text_for(tmp_path):
    """
    `license-files` must stay out when there is no file, or the build breaks.
    """
    root = project(tmp_path / "mypkg")
    assert write_license(root, "Apache-2.0", 2026) is False

    written = (root / "pyproject.toml").read_text()
    assert 'license = "Apache-2.0"' in written
    assert "license-files" not in written
    assert not (root / LICENSE_FILE).exists()


@pytest.mark.parametrize("license_id", [licenses.NO_LICENSE, ""])
def test_write_license_leaves_everything_alone_for_no_license(tmp_path, license_id):
    root = project(tmp_path / "mypkg")
    assert write_license(root, license_id, 2026) is False
    assert (root / "pyproject.toml").read_text() == PYPROJECT
    assert not (root / LICENSE_FILE).exists()


def test_copyright_holder_reads_the_name_uv_filled_in(tmp_path):
    root = project(tmp_path / "mypkg")
    assert copyright_holder(root, "the authors") == "Robin"


@pytest.mark.parametrize(
    "authors",
    ["", "authors = []", 'authors = [\n    { email = "x@example.com" }\n]'],
)
def test_copyright_holder_falls_back_without_a_usable_name(tmp_path, authors):
    root = project(tmp_path / "mypkg", f'[project]\nname = "mypkg"\n{authors}\n')
    assert copyright_holder(root, "the authors") == "the authors"


# --- gitignore ---------------------------------------------------------------


def test_ensure_gitignore_writes_one_when_uv_did_not(tmp_path):
    root = project(tmp_path / "mypkg")
    assert ensure_gitignore(root) is True
    assert (root / GITIGNORE).read_text() == FALLBACK_GITIGNORE
    assert "dist/" in FALLBACK_GITIGNORE


def test_ensure_gitignore_keeps_the_one_uv_wrote(tmp_path):
    root = project(tmp_path / "mypkg")
    (root / GITIGNORE).write_text("dist/\n")
    assert ensure_gitignore(root) is False
    assert (root / GITIGNORE).read_text() == "dist/\n"


# --- run_scaffold ------------------------------------------------------------


def test_run_scaffold_refuses_when_uv_init_fails(tmp_path):
    runner = FakeRunner().reply("uv init", returncode=1, stderr="already initialized")
    with pytest.raises(VommitError, match="already initialized"):
        run_scaffold(
            ScaffoldRequest(project_name="mypkg"),
            runner=runner,
            cwd=tmp_path,
            configure=lambda _: None,
        )


def test_run_scaffold_creates_configures_and_commits(tmp_path):
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)
    configured: list[Path] = []

    result = run_scaffold(
        ScaffoldRequest(
            project_name="mypkg",
            branch="main",
            license_id="MIT",
            commit_message="chore: initial commit",
        ),
        runner=runner,
        cwd=tmp_path,
        configure=configured.append,
        today=dt.date(2026, 7, 28),
    )

    assert result.root == root.resolve()
    assert result.branch == "main"
    assert result.license_id == "MIT"
    assert result.committed is True
    assert configured == [root.resolve()]

    repo = GitRepo(runner=LocalRunner(), root=root)
    assert repo.current_branch() == "main"
    assert repo.head_subject() == "chore: initial commit"
    assert "Copyright (c) 2026" in (root / LICENSE_FILE).read_text()


def test_run_scaffold_renames_the_branch_git_init_chose(tmp_path):
    """
    `uv init` has no say over the branch name; `init.defaultBranch` does.
    """
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root, branch="master")
    notes: list[str] = []

    result = run_scaffold(
        ScaffoldRequest(project_name="mypkg", branch="main"),
        runner=runner,
        cwd=tmp_path,
        configure=lambda _: None,
        notify=notes.append,
    )

    assert result.branch == "main"
    assert GitRepo(runner=LocalRunner(), root=root).current_branch() == "main"
    assert any("master" in note and "main" in note for note in notes)


def test_run_scaffold_leaves_a_matching_branch_alone(tmp_path):
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root, branch="main")
    run_scaffold(
        ScaffoldRequest(project_name="mypkg", branch="main"),
        runner=runner,
        cwd=tmp_path,
        configure=lambda _: None,
    )
    assert not runner.ran("branch -m")


def test_run_scaffold_configures_after_the_remote_so_the_branch_can_derive(tmp_path):
    """
    The ordering the whole module exists for: `origin` must be there before the
    config is written, or the release branch is a guess.
    """
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)
    order: list[str] = []

    def configure(_: Path) -> None:
        order.append("configure")

    original = runner.run

    def run(command: str, env: dict[str, str] | None = None) -> t.Any:
        if "remote add" in command:
            order.append("remote")
        if "commit -m" in command:
            order.append("commit")
        return original(command, env)

    runner.run = run  # type: ignore[method-assign]

    run_scaffold(
        ScaffoldRequest(
            project_name="mypkg",
            remote="git@example.com:me/mypkg.git",
            commit_message="chore: initial commit",
        ),
        runner=runner,
        cwd=tmp_path,
        configure=configure,
    )

    assert order == ["remote", "configure", "commit"]


def test_run_scaffold_pushes_with_an_upstream(tmp_path):
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)
    # not "push": pytest names tmp_path after the test, so that substring is in
    # every command carrying the path
    runner.reply("--set-upstream", stdout="")

    result = run_scaffold(
        ScaffoldRequest(
            project_name="mypkg",
            remote="git@example.com:me/mypkg.git",
            commit_message="chore: initial commit",
            push=True,
        ),
        runner=runner,
        cwd=tmp_path,
        configure=lambda _: None,
    )

    assert result.pushed is True
    assert runner.ran("push --set-upstream origin main")


def test_run_scaffold_does_not_push_without_a_commit(tmp_path):
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)

    result = run_scaffold(
        ScaffoldRequest(
            project_name="mypkg", remote="git@example.com:me/mypkg.git", push=True
        ),
        runner=runner,
        cwd=tmp_path,
        configure=lambda _: None,
    )

    assert result.committed is False
    assert result.pushed is False
    assert not runner.ran("--set-upstream")


def test_run_scaffold_keeps_an_existing_origin(tmp_path):
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)
    notes: list[str] = []

    def scaffold(url: str) -> t.Any:
        notes.clear()
        return run_scaffold(
            ScaffoldRequest(project_name="mypkg", remote=url),
            runner=runner,
            cwd=tmp_path,
            configure=lambda _: None,
            notify=notes.append,
        )

    assert scaffold("git@example.com:me/mypkg.git").remote.endswith("mypkg.git")

    # a second pass over the same directory finds `origin` already there
    result = scaffold("git@example.com:me/other.git")
    assert result.remote is None
    assert any("already exists" in note for note in notes)
    assert GitRepo(runner=LocalRunner(), root=root).remotes() == ["origin"]


def test_run_scaffold_syncs_when_asked(tmp_path):
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)
    notes: list[str] = []

    result = run_scaffold(
        ScaffoldRequest(project_name="mypkg", sync=True),
        runner=runner,
        cwd=tmp_path,
        configure=lambda _: None,
        notify=notes.append,
    )

    assert result.synced is True
    assert runner.ran(f"uv sync --directory {root.resolve()}")
    assert any("uv.lock" in note for note in notes)


def test_run_scaffold_reports_a_failed_sync(tmp_path):
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)
    runner.reply("uv sync", returncode=1, stderr="no interpreter")

    with pytest.raises(VommitError, match="no interpreter"):
        run_scaffold(
            ScaffoldRequest(project_name="mypkg", sync=True),
            runner=runner,
            cwd=tmp_path,
            configure=lambda _: None,
        )


def test_run_scaffold_leaves_an_enclosing_repository_alone(tmp_path):
    """
    `uv init` skips `git init` inside a repository, which also means no
    `.gitignore`: without one a release would commit its own `dist/`.
    """
    outer = LocalRunner()
    outer.run(shlex.join(["git", "init", "--initial-branch=trunk", str(tmp_path)]))

    root = tmp_path / "mypkg"
    runner = FakeRunner()
    real = LocalRunner()

    def run(command: str, env: dict[str, str] | None = None) -> t.Any:
        runner.calls.append(command)
        runner.envs.append(env)
        if " init --package" in command:
            project(root)
            return runner.default
        return real.run(command) if command.startswith("git ") else runner.default

    runner.run = run  # type: ignore[method-assign]
    notes: list[str] = []

    result = run_scaffold(
        ScaffoldRequest(
            project_name="mypkg",
            branch="main",
            remote="git@example.com:me/mypkg.git",
            commit_message="chore: initial commit",
        ),
        runner=runner,
        cwd=tmp_path,
        configure=lambda _: None,
        notify=notes.append,
    )

    assert result.branch == "trunk"
    assert result.remote is None
    assert result.committed is False
    assert not runner.ran("branch -m")
    assert not runner.ran("remote add")
    assert not runner.ran("commit -m")
    assert (root / GITIGNORE).read_text() == FALLBACK_GITIGNORE
    assert any("existing git repository" in note for note in notes)
    assert any(GITIGNORE in note for note in notes)


def test_ignore_is_a_notify_that_does_nothing():
    assert ignore("anything") is None
