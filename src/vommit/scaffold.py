"""
Creating a new project: `uv init`, then the steps that make it releasable.

The ordering here is the whole point of the module. `uv init` writes the
project, the remote is added before the vommit config is written so the branch
can be derived from it, and the first commit is made after that config exists so
one commit holds the whole scaffold.
"""

import dataclasses as dc
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

#: The one signal that means "the user picked this environment".
ENVIRONMENT_VAR = "VIRTUAL_ENV"

#: What `.gitignore` has to say for a release not to commit its own build or a
#: virtual environment. Checked against whatever is already there.
REQUIRED_IGNORES: list[str] = [
    "__pycache__/",
    "*.py[oc]",
    "build/",
    "dist/",
    "wheels/",
    "*.egg-info",
    ".venv",
    "venv/",
]

#: Written whole when there is no `.gitignore` at all.
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
venv/
"""


def ignore(_: str) -> None:
    """
    The default for callers with nowhere to report progress.
    """


@dc.dataclass(frozen=True)
class InstallTarget:
    """
    Where a new project can be installed, or why it cannot be.

    `refused` carries the reason so the caller can say it out loud; an absent
    target that explains itself beats one that quietly does nothing.
    """

    path: str | None = None
    refused: str = ""


def install_target(environ: t.Mapping[str, str], own_prefix: str) -> InstallTarget:
    """
    The environment to install a new project into, decided here rather than by uv.

    Only `VIRTUAL_ENV` counts, which is set by `activate` and by `uv run` and by
    nothing else. uv's own resolution would go on to read `CONDA_PREFIX` and then
    hunt for a directory named `.venv` by walking up from the project -- which
    silently finds an enclosing project's environment, and misses a `venv/`
    sitting right there. Neither is a choice anybody made.

    `own_prefix` is where vommit itself is installed, and it is never the answer.
    Working on vommit means having that environment activated, and every `vommit
    init` run from such a shell would otherwise offer to install a brand new
    unrelated package into vommit's own site-packages.
    """
    active = environ.get(ENVIRONMENT_VAR)
    if not active:
        return InstallTarget(refused=f"${ENVIRONMENT_VAR} is not set")
    if _same_directory(active, own_prefix):
        return InstallTarget(
            refused=f"{active} is the environment vommit itself runs from"
        )
    return InstallTarget(path=active)


def _same_directory(one: str, other: str) -> bool:
    """
    Whether two paths name the same place, symlinks and `..` included.
    """
    return Path(one).resolve() == Path(other).resolve()


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
    #: The environment to install into; None leaves the project uninstalled.
    environment: str | None = None

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
    installed: bool = False


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
    environment: str | None = None,
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
        environment=_settle_environment(defaults, asker, environment),
    )


def _settle_environment(
    defaults: ScaffoldRequest,
    asker: Asker,
    environment: str | None,
) -> str | None:
    """
    Where to install, asked by name rather than by description.

    Nothing to install into means nothing to ask: the question would offer an
    action that could only fail. Naming the path is the point -- `uv run vommit`
    sets `VIRTUAL_ENV` to the environment vommit itself runs from, and seeing
    that path is what makes it an obvious no.
    """
    if not environment:
        return None
    wanted = defaults.environment is not None
    if asker.confirm(f"Install {defaults.project_name} into {environment}?", wanted):
        return environment
    return None


def declare_license(root: Path, license_id: str) -> bool:
    """
    Record the chosen license in `[project].license`, and nothing else.

    `license-files` stays out: it would have to name a LICENSE file, and writing
    that file means carrying license texts, which is the author's call to make
    rather than ours to embed.
    """
    if license_id in {licenses.NO_LICENSE, ""}:
        return False

    pyproject = root / PYPROJECT
    document = read_toml(pyproject)
    project = document["project"]
    project["license"] = license_id  # type: ignore[index]
    pyproject.write_text(document.as_string())
    return True


def ensure_gitignore(root: Path) -> list[str]:
    """
    Make sure build output and environments are ignored, and say what was added.

    uv writes a `.gitignore` only when it inits git, so inside an existing
    repository there is none and a release would commit its own `dist/`. The one
    uv does write is missing `venv/`, so an existing file is topped up rather
    than trusted; everything already in it is left as it stands.
    """
    path = root / GITIGNORE
    if not path.exists():
        path.write_text(FALLBACK_GITIGNORE)
        return REQUIRED_IGNORES

    present = set(path.read_text().splitlines())
    missing = [entry for entry in REQUIRED_IGNORES if entry not in present]
    if missing:
        existing = path.read_text()
        separator = "" if existing.endswith("\n") else "\n"
        path.write_text(existing + separator + "\n".join(missing) + "\n")
    return missing


def run_scaffold(
    request: ScaffoldRequest,
    runner: Runner,
    cwd: Path,
    configure: Configure,
    notify: Notify = ignore,
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
    if added := ensure_gitignore(root):
        notify(f"Added to {GITIGNORE}: {', '.join(added)}.")

    branch = _settle_branch(repo, request.branch, own_repo, notify)
    if declare_license(root, request.license_id):
        notify(
            f"Recorded {request.license_id} in [project].license; "
            f"the {LICENSE_FILE} file is yours to add."
        )

    remote = _settle_remote(repo, request.remote, own_repo, notify)
    configure(root)

    committed = _commit(repo, request.commit_message, own_repo, notify)
    pushed = bool(remote) and committed and request.push
    if pushed:
        repo.push_upstream(ORIGIN, branch)
        notify(f"Pushed '{branch}' to {remote}.")

    environment = request.environment
    installed = bool(environment) and _install(runner, root, environment, notify)

    declared = request.license_id
    return ScaffoldResult(
        root=root,
        branch=branch,
        license_id=declared if declared != licenses.NO_LICENSE else None,
        remote=remote,
        committed=committed,
        pushed=pushed,
        installed=installed,
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


def _install(runner: Runner, root: Path, environment: str, notify: Notify) -> bool:
    """
    Install the new project into `environment`, editable.

    `--python` names the target, so uv resolves nothing: no `VIRTUAL_ENV`, no
    `CONDA_PREFIX`, no walking up the tree looking for a `.venv`. It overrides
    all three.

    `uv pip install` rather than `uv sync`, which would create a `.venv` and a
    `uv.lock` inside a project that asked for neither.

    Never fatal: this runs last, after the commit and the push, so the project is
    already made and reporting beats unwinding nothing.
    """
    result = runner.run(
        shlex.join(
            [
                "uv",
                "pip",
                "install",
                "--python",
                environment,
                "--directory",
                str(root),
                "-e",
                ".",
            ]
        )
    )
    if not result.ok:
        notify(f"Could not install into {environment}: {result.error}")
        return False

    notify(f"Installed into {environment}, editable.")
    return True
