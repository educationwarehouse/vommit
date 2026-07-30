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
    INITIAL_VERSION,
    FALLBACK_GITIGNORE,
    GITIGNORE,
    LICENSE_FILE,
    NO_VENV,
    VENV_CHOICES,
    initial_commit_message,
    wanted_ignores,
    REQUIRED_IGNORES,
    Defaults,
    ScaffoldRequest,
    default_python,
    ensure_gitignore,
    ignore,
    plan_request,
    run_scaffold,
    uv_init_argv,
    declare_license,
    reset_version,
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
        venv="venv",
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


def test_plan_request_keeps_a_license_it_does_not_offer():
    """
    Regression: an identifier outside CHOICES became MIT under `Defaults`, so
    `--license EUPL-1.2 --non-interactive` declared the package under MIT.
    """
    assert "EUPL-1.2" not in licenses.CHOICES

    planned = plan_request(
        ScaffoldRequest(project_name="mypkg", license_id="EUPL-1.2"), Defaults()
    )
    assert planned.license_id == "EUPL-1.2"


def test_plan_request_carries_an_unoffered_license_through_other():
    """
    It cannot be the select's default -- that would open on a choice absent from
    the list -- so it rides on `other` and pre-fills the follow-up, which is also
    how a person gets to see and change it.
    """
    asker = ScriptedAsker()
    plan_request(ScaffoldRequest(project_name="mypkg", license_id="EUPL-1.2"), asker)

    assert asker.asked["License"] == licenses.OTHER_LICENSE
    assert asker.asked["SPDX license identifier"] == "EUPL-1.2"


def test_plan_request_lets_a_person_pick_a_listed_license_instead():
    planned = plan_request(
        ScaffoldRequest(project_name="mypkg", license_id="EUPL-1.2"),
        ScriptedAsker({"License": "MIT"}),
    )
    assert planned.license_id == "MIT"


def test_plan_request_offers_a_listed_license_id_as_given():
    asker = ScriptedAsker()
    plan_request(ScaffoldRequest(project_name="mypkg", license_id="Apache-2.0"), asker)
    assert asker.asked["License"] == "Apache-2.0"
    assert "SPDX license identifier" not in asker.asked


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


def test_plan_request_asks_for_the_venv_directory():
    asker = ScriptedAsker({"Create a virtual environment?": ".venv"})
    planned = plan_request(ScaffoldRequest(project_name="mypkg", venv="venv"), asker)

    assert planned.venv == ".venv"
    assert asker.asked["Create a virtual environment?"] == "venv"


def test_plan_request_offers_none_as_a_choice():
    """
    "none" has to be answerable, not merely absent from the offered names.
    """
    assert NO_VENV in VENV_CHOICES

    planned = plan_request(
        ScaffoldRequest(project_name="mypkg", venv="venv"),
        ScriptedAsker({"Create a virtual environment?": NO_VENV}),
    )
    assert planned.venv is None


def test_plan_request_offers_none_when_the_flag_declined_one():
    asker = ScriptedAsker()
    planned = plan_request(ScaffoldRequest(project_name="mypkg"), asker)

    assert asker.asked["Create a virtual environment?"] == NO_VENV
    assert planned.venv is None


def test_request_refuses_a_venv_outside_the_project():
    for name in ["/tmp/elsewhere", "../sneaky", "nested/venv"]:
        with pytest.raises(VommitError, match="plain directory name"):
            ScaffoldRequest(project_name="mypkg", venv=name)


# --- version ------------------------------------------------------------------


def test_reset_version_undoes_uvs_head_start(tmp_path):
    """
    `uv init` writes 0.1.0, which claims a release that never happened: the
    first bump would then go to 0.2.0 and leave 0.1.0 uninstallable.
    """
    root = project(tmp_path / "mypkg")

    assert reset_version(root) is True
    assert f'version = "{INITIAL_VERSION}"' in (root / "pyproject.toml").read_text()


def test_reset_version_leaves_an_already_unreleased_project_alone(tmp_path):
    root = project(
        tmp_path / "mypkg", f'[project]\nname = "mypkg"\nversion = "{INITIAL_VERSION}"\n'
    )
    before = (root / "pyproject.toml").read_text()

    assert reset_version(root) is False
    assert (root / "pyproject.toml").read_text() == before


def test_reset_version_without_a_project_table(tmp_path):
    root = project(tmp_path / "mypkg", "[tool.other]\nkey = 1\n")

    assert reset_version(root) is False


# --- licenses ----------------------------------------------------------------


def test_declare_license_records_the_identifier_only(tmp_path):
    """
    No LICENSE file and no `license-files`: the text is the author's to choose.
    """
    root = project(tmp_path / "mypkg")
    assert declare_license(root, "MIT") is True

    written = (root / "pyproject.toml").read_text()
    assert 'license = "MIT"' in written
    assert "license-files" not in written
    assert not (root / LICENSE_FILE).exists()


def test_declare_license_takes_an_identifier_with_no_carried_text(tmp_path):
    root = project(tmp_path / "mypkg")
    assert declare_license(root, "Apache-2.0") is True
    assert 'license = "Apache-2.0"' in (root / "pyproject.toml").read_text()


@pytest.mark.parametrize("license_id", [licenses.NO_LICENSE, ""])
def test_declare_license_leaves_everything_alone_for_no_license(tmp_path, license_id):
    root = project(tmp_path / "mypkg")
    assert declare_license(root, license_id) is False
    assert (root / "pyproject.toml").read_text() == PYPROJECT
    assert not (root / LICENSE_FILE).exists()


def test_declare_license_writes_into_a_table_split_by_other_sections(tmp_path):
    """
    `[project]` interrupted by another section is an out-of-order table rather
    than a plain one; it is still where the license belongs.
    """
    root = project(
        tmp_path / "mypkg",
        '[project]\nname = "mypkg"\nversion = "0.0.0"\n\n'
        '[build-system]\nrequires = ["uv_build"]\n\n'
        "[project.optional-dependencies]\ndev = []\n",
    )
    assert declare_license(root, "MIT") is True
    assert 'license = "MIT"' in (root / "pyproject.toml").read_text()


@pytest.mark.parametrize("content", ['project = "mypkg"\n', 'name = "mypkg"\n'])
def test_declare_license_refuses_a_pyproject_without_a_project_table(
    tmp_path, content
):
    root = project(tmp_path / "mypkg", content)
    with pytest.raises(VommitError, match=r"\[project\] must be a TOML table"):
        declare_license(root, "MIT")


def test_every_offered_license_is_a_bare_identifier():
    """
    The choices are SPDX strings, not a table of texts to keep in step.
    """
    assert licenses.DEFAULT_LICENSE in licenses.COMMON
    assert licenses.CHOICES[-2:] == [licenses.OTHER_LICENSE, licenses.NO_LICENSE]


# --- commit message -----------------------------------------------------------


def test_initial_commit_message_defaults_when_the_flag_is_absent():
    assert initial_commit_message(None) == DEFAULT_COMMIT_MESSAGE


@pytest.mark.parametrize("supplied", ["", "   "])
def test_initial_commit_message_honours_an_explicitly_empty_one(supplied):
    """
    Regression: `--message ''` fell back to the default, which left
    `--non-interactive` no way to skip the commit at all.
    """
    assert initial_commit_message(supplied) is None


def test_initial_commit_message_keeps_what_was_given():
    assert initial_commit_message("  chore: start  ") == "chore: start"


def test_declining_the_commit_declines_the_push_with_it():
    planned = plan_request(
        ScaffoldRequest(
            project_name="mypkg",
            commit_message=initial_commit_message(""),
            remote="git@example.com:me/mypkg.git",
            push=True,
        ),
        Defaults(),
    )
    assert planned.commit_message is None
    assert planned.push is False


# --- gitignore ---------------------------------------------------------------


def test_ensure_gitignore_writes_one_when_there_is_none(tmp_path):
    root = project(tmp_path / "mypkg")

    assert ensure_gitignore(root) == REQUIRED_IGNORES

    written = (root / GITIGNORE).read_text()
    assert written == FALLBACK_GITIGNORE
    for entry in REQUIRED_IGNORES:
        assert entry in written


def test_ensure_gitignore_tops_up_the_one_uv_wrote(tmp_path):
    """
    uv's own `.gitignore` has `.venv` but not `venv/`.
    """
    root = project(tmp_path / "mypkg")
    (root / GITIGNORE).write_text("dist/\n.venv\n")

    assert ensure_gitignore(root) == [
        "__pycache__/",
        "*.py[oc]",
        "build/",
        "wheels/",
        "*.egg-info",
        "venv/",
    ]

    lines = (root / GITIGNORE).read_text().splitlines()
    assert lines[:2] == ["dist/", ".venv"]
    assert set(REQUIRED_IGNORES) <= set(lines)


@pytest.mark.parametrize("venv", ["venv", ".venv", None])
def test_wanted_ignores_adds_nothing_for_the_offered_names(venv):
    assert wanted_ignores(venv) == REQUIRED_IGNORES


def test_wanted_ignores_covers_a_custom_environment_directory():
    """
    A `.gitignore` naming `venv/` while the project keeps its environment in
    `env/` is wrong on its face, whatever uv's own nested ignore happens to do.
    """
    assert wanted_ignores("env") == [*REQUIRED_IGNORES, "env/"]


def test_ensure_gitignore_ignores_a_custom_environment_directory(tmp_path):
    root = project(tmp_path / "mypkg")
    (root / GITIGNORE).write_text(FALLBACK_GITIGNORE)

    assert ensure_gitignore(root, "env") == ["env/"]
    assert "env/" in (root / GITIGNORE).read_text().splitlines()


def test_ensure_gitignore_writes_a_custom_directory_into_a_fresh_file(tmp_path):
    root = project(tmp_path / "mypkg")

    assert ensure_gitignore(root, "env") == [*REQUIRED_IGNORES, "env/"]

    lines = (root / GITIGNORE).read_text().splitlines()
    assert set(REQUIRED_IGNORES) <= set(lines)
    assert "env/" in lines


def test_ensure_gitignore_adds_a_newline_before_appending(tmp_path):
    root = project(tmp_path / "mypkg")
    (root / GITIGNORE).write_text("dist/")

    ensure_gitignore(root)

    assert (root / GITIGNORE).read_text().splitlines()[0] == "dist/"


def test_ensure_gitignore_changes_nothing_when_everything_is_there(tmp_path):
    root = project(tmp_path / "mypkg")
    (root / GITIGNORE).write_text(FALLBACK_GITIGNORE)

    assert ensure_gitignore(root) == []
    assert (root / GITIGNORE).read_text() == FALLBACK_GITIGNORE


# --- run_scaffold ------------------------------------------------------------


def test_run_scaffold_refuses_when_uv_init_fails(tmp_path):
    runner = FakeRunner().reply("uv init", returncode=1, stderr="already initialized")
    with pytest.raises(VommitError, match="already initialized"):
        run_scaffold(
            ScaffoldRequest(project_name="mypkg"),
            runner=runner,
            cwd=tmp_path,
            configure=lambda *_: None,
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
        configure=lambda root, _: configured.append(root),
    )

    assert result.root == root.resolve()
    assert result.branch == "main"
    assert result.license_id == "MIT"
    assert result.committed is True
    assert configured == [root.resolve()]

    repo = GitRepo(runner=LocalRunner(), root=root)
    assert repo.current_branch() == "main"
    assert repo.head_subject() == "chore: initial commit"
    written = (root / "pyproject.toml").read_text()
    assert 'license = "MIT"' in written
    assert f'version = "{INITIAL_VERSION}"' in written
    assert not (root / LICENSE_FILE).exists()


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
        configure=lambda *_: None,
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
        configure=lambda *_: None,
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

    def configure(_: Path, __: str) -> None:
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
        configure=lambda *_: None,
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
        configure=lambda *_: None,
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
            configure=lambda *_: None,
            notify=notes.append,
        )

    assert scaffold("git@example.com:me/mypkg.git").remote.endswith("mypkg.git")

    # a second pass over the same directory finds `origin` already there
    result = scaffold("git@example.com:me/other.git")
    assert result.remote is None
    assert any("already exists" in note for note in notes)
    assert GitRepo(runner=LocalRunner(), root=root).remotes() == ["origin"]


def test_run_scaffold_creates_the_venv_and_installs_into_it(tmp_path):
    """
    `--python` names the target, so uv resolves nothing: not VIRTUAL_ENV, not
    CONDA_PREFIX, and no walking up the tree for a directory named `.venv`.
    """
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)
    notes: list[str] = []

    result = run_scaffold(
        ScaffoldRequest(project_name="mypkg", venv="venv", python="3.13"),
        runner=runner,
        cwd=tmp_path,
        configure=lambda *_: None,
        notify=notes.append,
    )

    assert result.venv == "venv"
    assert result.installed is True
    assert runner.ran(f"uv venv --directory {root.resolve()} --python 3.13 venv")
    assert runner.ran(
        f"uv pip install --python {root.resolve() / 'venv'} "
        f"--directory {root.resolve()} -e ."
    )
    # `uv sync` would write a uv.lock the project did not ask for
    assert not runner.ran("uv sync")
    assert any("venv" in note for note in notes)


def test_run_scaffold_hands_the_settled_branch_to_configure(tmp_path):
    """
    Regression: the remote goes in before the config is written, so a remote
    whose HEAD names another branch used to win and configure a release from a
    branch the project was not on.
    """
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root, branch="master")
    seen: list[str] = []

    run_scaffold(
        ScaffoldRequest(
            project_name="mypkg",
            branch="release",
            remote="git@example.com:me/mypkg.git",
        ),
        runner=runner,
        cwd=tmp_path,
        configure=lambda _, settled: seen.append(settled),
    )

    assert seen == ["release"]


def test_run_scaffold_ignores_the_environment_before_committing(tmp_path):
    """
    The ignore has to land in the commit, not after it.
    """
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)
    order: list[str] = []
    original = runner.run

    def run(command: str, env: dict[str, str] | None = None) -> t.Any:
        if "commit -m" in command:
            order.append("commit")
        if "uv venv" in command:
            order.append("venv")
        return original(command, env)

    runner.run = run  # type: ignore[method-assign]

    run_scaffold(
        ScaffoldRequest(
            project_name="mypkg", venv="env", commit_message="chore: initial commit"
        ),
        runner=runner,
        cwd=tmp_path,
        configure=lambda *_: None,
    )

    assert order == ["commit", "venv"]
    assert "env/" in (root / GITIGNORE).read_text().splitlines()


def test_run_scaffold_makes_no_venv_when_none_was_asked_for(tmp_path):
    runner = uv_init_faker(tmp_path / "mypkg")

    result = run_scaffold(
        ScaffoldRequest(project_name="mypkg"),
        runner=runner,
        cwd=tmp_path,
        configure=lambda *_: None,
    )

    assert result.venv is None
    assert result.installed is False
    assert not runner.ran("uv venv")
    assert not runner.ran("uv pip install")


def test_run_scaffold_skips_the_install_when_the_venv_fails(tmp_path):
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)
    runner.reply("uv venv", returncode=1, stderr="no such interpreter")
    notes: list[str] = []

    result = run_scaffold(
        ScaffoldRequest(project_name="mypkg", venv="venv"),
        runner=runner,
        cwd=tmp_path,
        configure=lambda *_: None,
        notify=notes.append,
    )

    assert result.venv is None
    assert result.installed is False
    assert not runner.ran("uv pip install")
    assert any("no such interpreter" in note for note in notes)


def test_run_scaffold_reports_a_failed_install_without_failing(tmp_path):
    """
    Both steps run after the commit and the push, so there is nothing left to
    unwind and reporting beats raising.
    """
    root = tmp_path / "mypkg"
    runner = uv_init_faker(root)
    runner.reply("uv pip install", returncode=1, stderr="resolution failed")
    notes: list[str] = []

    result = run_scaffold(
        ScaffoldRequest(
            project_name="mypkg",
            venv="venv",
            commit_message="chore: initial commit",
        ),
        runner=runner,
        cwd=tmp_path,
        configure=lambda *_: None,
        notify=notes.append,
    )

    assert result.venv == "venv"
    assert result.installed is False
    assert result.committed is True
    assert any("resolution failed" in note for note in notes)


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
        configure=lambda *_: None,
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
