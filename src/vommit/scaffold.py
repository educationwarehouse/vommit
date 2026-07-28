"""
Creating a new project: `uv init`, then the steps that make it releasable.

The ordering here is the whole point of the module. `uv init` writes the
project, the remote is added before the vommit config is written so the branch
can be derived from it, and the first commit is made after that config exists so
one commit holds the whole scaffold.
"""

import dataclasses as dc
import datetime as dt
import shlex
import sys
import typing as t
from pathlib import Path

from . import licenses
from .errors import VommitError
from .git import GitRepo
from .helpers import read_toml
from .shell import Runner

Notify = t.Callable[[str], None]

#: Writes the vommit config into the finished project; `setup`, in practice.
Configure = t.Callable[[Path], None]

PYPROJECT = "pyproject.toml"
GITIGNORE = ".gitignore"
LICENSE_FILE = "LICENSE"
ORIGIN = "origin"
DEFAULT_BRANCH = "main"
DEFAULT_COMMIT_MESSAGE = "chore: initial commit"

#: What `.gitignore` needs to say for a release not to commit its own build.
FALLBACK_GITIGNORE = """\
# Python-generated files
__pycache__/
*.py[oc]
build/
dist/
wheels/
*.egg-info

# Virtual environments
.venv
"""


def ignore(_: str) -> None:
    """
    The default for callers with nowhere to report progress.
    """


def default_python() -> str:
    """
    The interpreter running vommit, as a `major.minor` floor.
    """
    return f"{sys.version_info.major}.{sys.version_info.minor}"


class Asker(t.Protocol):
    """
    The questions `init` asks. Separate from the prompting so the flow is
    testable without a terminal.
    """

    def text(
        self, question: str, default: str = ""
    ) -> str: ...  # pragma: no cover

    def confirm(
        self, question: str, default: bool = True
    ) -> bool: ...  # pragma: no cover

    def choose(
        self, question: str, options: list[str], default: str
    ) -> str: ...  # pragma: no cover


@dc.dataclass(frozen=True)
class Defaults:
    """
    An asker that answers with the default; what `--non-interactive` uses.
    """

    def text(self, question: str, default: str = "") -> str:
        return default

    def confirm(self, question: str, default: bool = True) -> bool:
        return default

    def choose(self, question: str, options: list[str], default: str) -> str:
        return default


@dc.dataclass(frozen=True)
class ScaffoldRequest:
    """
    Everything `init` needs to know before it touches the disk.
    """

    project_name: str
    python: str | None = None
    description: str | None = None
    license_id: str = licenses.NO_LICENSE
    branch: str = DEFAULT_BRANCH
    pin_python: bool = False
    workspace: bool = True
    remote: str | None = None
    commit_message: str | None = None
    push: bool = False
    sync: bool = False

    def __post_init__(self) -> None:
        if not self.project_name.strip():
            raise VommitError("A project name is required.")
        if not self.branch.strip():
            raise VommitError("A branch name is required.")
        if self.push and not self.remote:
            raise VommitError(
                "Pushing needs a remote; give one, or leave the push out."
            )


@dc.dataclass(frozen=True)
class ScaffoldResult:
    """
    What `init` ended up doing, for the report at the end.
    """

    root: Path
    branch: str
    license_id: str | None = None
    remote: str | None = None
    committed: bool = False
    pushed: bool = False
    synced: bool = False


def uv_init_argv(request: ScaffoldRequest, root: Path) -> list[str]:
    """
    The `uv init` invocation for `request`.

    `--no-pin-python` unless asked: uv would pin whichever interpreter happened
    to be on PATH, and a `.python-version` naming the wrong one silently holds
    `uv run` back. An empty description becomes `--no-description` rather than
    uv's "Add your description here", which would otherwise reach PyPI.
    """
    argv = ["uv", "init", "--package"]
    if not request.pin_python:
        argv.append("--no-pin-python")
    if request.python:
        argv += ["--python", request.python]
    if request.description:
        argv += ["--description", request.description]
    else:
        argv.append("--no-description")
    if not request.workspace:
        argv.append("--no-workspace")
    argv.append(str(root))
    return argv


def plan_request(
    defaults: ScaffoldRequest,
    asker: Asker,
    detected_branch: str | None = None,
) -> ScaffoldRequest:
    """
    Ask what `uv init` and the git steps need, in the order they run.

    Every answer starts from `defaults`, so a flag given on the command line
    pre-fills its prompt, and `Defaults` hands back the request unchanged. The
    remote is asked here rather than after the project exists: a remote in place
    before the config is written is what lets the release branch be derived
    instead of guessed.
    """
    description = asker.text("Description", defaults.description or "").strip()
    floor = asker.text(
        "Minimum Python version", defaults.python or default_python()
    ).strip()

    offered = defaults.license_id if defaults.license_id in licenses.CHOICES else None
    license_id = asker.choose(
        "License", licenses.CHOICES, offered or licenses.DEFAULT_LICENSE
    )
    if license_id == licenses.OTHER_LICENSE:
        license_id = asker.text("SPDX license identifier", "").strip()

    branch_default = detected_branch or defaults.branch
    branch = asker.text("Release branch", branch_default).strip() or branch_default
    remote = asker.text("Git remote URL (empty for none)", defaults.remote or "").strip()

    wanted = defaults.commit_message or DEFAULT_COMMIT_MESSAGE
    commit = (
        asker.text("Initial commit message", wanted).strip()
        if asker.confirm("Make an initial commit?", defaults.commit_message is not None)
        else ""
    )
    push = (
        bool(remote)
        and bool(commit)
        and asker.confirm(f"Push to {remote}?", defaults.push)
    )

    return ScaffoldRequest(
        project_name=defaults.project_name,
        python=floor or None,
        description=description or None,
        license_id=license_id or licenses.NO_LICENSE,
        branch=branch,
        pin_python=defaults.pin_python,
        workspace=defaults.workspace,
        remote=remote or None,
        commit_message=commit or None,
        push=push,
        sync=asker.confirm("Run `uv sync` when it is set up?", defaults.sync),
    )


def copyright_holder(root: Path, fallback: str) -> str:
    """
    The name `uv init` filled in from git, for the license text.
    """
    pyproject = root / PYPROJECT
    if not pyproject.exists():  # pragma: no cover - uv wrote it or we stopped
        return fallback
    authors = read_toml(pyproject).get("project", {}).get("authors") or []
    for author in authors:
        if name := str(author.get("name", "")).strip():
            return name
    return fallback


def write_license(root: Path, license_id: str, year: int) -> bool:
    """
    Write the LICENSE file and the metadata that points at it.

    Returns whether a text was written: an identifier we do not carry still
    lands in `[project].license`, but claiming `license-files` without the file
    would break the build.
    """
    if license_id in {licenses.NO_LICENSE, ""}:
        return False

    text = licenses.render(license_id, copyright_holder(root, "the authors"), year)
    if text:
        (root / LICENSE_FILE).write_text(text)

    pyproject = root / PYPROJECT
    document = read_toml(pyproject)
    project = document["project"]
    project["license"] = license_id  # type: ignore[index]
    if text:
        project["license-files"] = [LICENSE_FILE]  # type: ignore[index]
    pyproject.write_text(document.as_string())
    return bool(text)


def ensure_gitignore(root: Path) -> bool:
    """
    Make sure build output is ignored, which uv only does when it inits git.

    Without this, `init` inside an existing repository leaves a project whose
    release would commit its own `dist/`.
    """
    path = root / GITIGNORE
    if path.exists():
        return False
    path.write_text(FALLBACK_GITIGNORE)
    return True


def run_scaffold(
    request: ScaffoldRequest,
    runner: Runner,
    cwd: Path,
    configure: Configure,
    notify: Notify = ignore,
    today: dt.date | None = None,
) -> ScaffoldResult:
    """
    Create the project, configure it, and take the git steps that were agreed.
    """
    root = (cwd / request.project_name).resolve()
    result = runner.run(shlex.join(uv_init_argv(request, root)))
    if not result.ok:
        raise VommitError(f"Could not create the project: {result.error}")

    repo = GitRepo(runner=runner, root=root)
    own_repo = repo.is_repo_root()
    if not own_repo:
        notify(
            f"{root.name} sits inside an existing git repository; "
            "leaving its history, branch and remote alone."
        )
    if ensure_gitignore(root):
        notify(f"Wrote {GITIGNORE}, so a release does not commit its own build.")

    branch = _settle_branch(repo, request.branch, own_repo, notify)
    licensed = write_license(root, request.license_id, (today or dt.date.today()).year)
    if licensed:
        notify(f"Wrote {LICENSE_FILE} ({request.license_id}).")

    remote = _settle_remote(repo, request.remote, own_repo, notify)
    configure(root)

    committed = _commit(repo, request.commit_message, own_repo, notify)
    pushed = bool(remote) and committed and request.push
    if pushed:
        repo.push_upstream(ORIGIN, branch)
        notify(f"Pushed '{branch}' to {remote}.")

    if request.sync:
        _sync(runner, root, notify)

    declared = request.license_id
    return ScaffoldResult(
        root=root,
        branch=branch,
        license_id=declared if declared != licenses.NO_LICENSE else None,
        remote=remote,
        committed=committed,
        pushed=pushed,
        synced=request.sync,
    )


def _settle_branch(
    repo: GitRepo,
    wanted: str,
    own_repo: bool,
    notify: Notify,
) -> str:
    """
    Put the new repository on the branch that was asked for.
    """
    current = repo.current_branch()
    if not own_repo or current == wanted:
        return current
    repo.rename_branch(wanted)
    notify(f"Renamed the branch from '{current}' to '{wanted}'.")
    return wanted


def _settle_remote(
    repo: GitRepo,
    url: str | None,
    own_repo: bool,
    notify: Notify,
) -> str | None:
    """
    Add the remote before the config is written, so the branch can be derived.
    """
    if not url or not own_repo:
        return None
    if ORIGIN in repo.remotes():
        notify(f"Remote '{ORIGIN}' already exists; leaving it as it is.")
        return None
    repo.add_remote(ORIGIN, url)
    notify(f"Added remote '{ORIGIN}' -> {url}.")
    return url


def _commit(
    repo: GitRepo,
    message: str | None,
    own_repo: bool,
    notify: Notify,
) -> bool:
    """
    Commit the whole scaffold, config and changelog included.
    """
    if not message or not own_repo:
        return False
    repo.add(["."])
    repo.commit(message)
    notify(f"Committed the scaffold as '{message}'.")
    return True


def _sync(runner: Runner, root: Path, notify: Notify) -> None:
    """
    Create the environment and install the project into it, editable.

    `uv sync` rather than `uv pip install -e .`, which needs an environment that
    does not exist yet in a project this new.
    """
    result = runner.run(shlex.join(["uv", "sync", "--directory", str(root)]))
    if not result.ok:
        raise VommitError(f"Could not sync the project: {result.error}")
    notify("Synced the project; `.venv` and `uv.lock` are in place.")
