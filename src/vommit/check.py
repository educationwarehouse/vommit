"""
`vommit check`: what vommit makes of this project, without changing anything.

Which kind of project it is, where the version lives, whether the config is
complete, what a release would run and whether it has a credential to publish
with. Read-only throughout: no prompt, no network, no write.
"""

import dataclasses as dc
import typing as t
from pathlib import Path

from configuraptor.errors import ConfigError

from . import cargo, npm
from .auth import ENVIRONMENT, TokenStore
from .build import project_warnings
from .config import Config
from .errors import VommitError
from .helpers import relative_path
from .release import PUBLISH, planned_steps
from .shell import Runner
from .versioning import (
    PYPROJECT,
    CargoProject,
    CrateProject,
    NpmProject,
    VersionSource,
    version_source,
)

Status = t.Literal["ok", "warn", "fail", "info"]

# how many unconfigured settings are listed by name before only counting them
MAX_NAMED = 5


@dc.dataclass(frozen=True)
class Finding:
    status: Status
    label: str
    detail: str


@dc.dataclass(frozen=True)
class CheckReport:
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        """
        Whether a release could run as configured; warnings do not count.
        """
        return all(finding.status != "fail" for finding in self.findings)


def project_kind(project: VersionSource) -> str:
    # CrateProject first: it is a CargoProject too
    if isinstance(project, CrateProject):
        return "Rust workspace" if project.is_workspace else "Rust crate"
    if isinstance(project, CargoProject):
        return "Python, built from a crate by maturin"
    if isinstance(project, NpmProject):
        return "npm package"
    return "Python (uv)"


def registry(project: VersionSource) -> str:
    if isinstance(project, CrateProject):
        return "crates.io"
    if isinstance(project, NpmProject):
        return "npm"
    return "PyPI"


def run_check(
    root: Path,
    runner: Runner,
    store: TokenStore | None = None,
) -> CheckReport:
    findings: list[Finding] = []
    pyproject = root / PYPROJECT

    try:
        project = version_source(runner, root)
    except VommitError as error:
        findings.append(Finding("fail", "Project", str(error)))
        return CheckReport(tuple(findings))

    findings.append(Finding("info", "Project", project_kind(project)))
    findings.append(_version(project, root))

    config, config_finding = _config(root, pyproject)
    findings.append(config_finding)
    if config is None:
        return CheckReport(tuple(findings))

    findings.append(_git(config))
    steps = planned_steps(config, project)
    findings.append(
        Finding(
            "info" if steps else "warn",
            "Release runs",
            "; ".join(f"{step.name}: {step.command}" for step in steps)
            or "no commands; a release only bumps (and pushes)",
        )
    )
    if any(step.name == PUBLISH for step in steps):
        findings.append(_credential(project, config, store or TokenStore()))

    findings.extend(
        Finding("warn", f"Build check {warning.id}", warning.message)
        for warning in project_warnings(root, config)
    )
    return CheckReport(tuple(findings))


def _version(project: VersionSource, root: Path) -> Finding:
    where = relative_path(project.manifest, root)
    if version := project.current_version():
        return Finding("ok", "Version", f"{version}, from {where}")
    return Finding("fail", "Version", f"none found in {where}")


def _config(root: Path, pyproject: Path) -> tuple[Config | None, Finding]:
    if not Config.has_pyproject_config(pyproject):
        return None, Finding(
            "fail", "Config", "no [tool.vommit] in pyproject.toml; run `vommit setup`"
        )
    try:
        config = Config.from_pyproject(root)
    except ConfigError as error:
        return None, Finding("fail", "Config", f"could not be loaded: {error}")

    missing = {
        path
        for path in Config.interactive_key_paths()
        - Config.configured_key_paths(pyproject)
        if not _switched_off(config, path)
    }
    if missing:
        # named while that is still readable; past it, a list of twenty keys
        # says less than the count does
        named = (
            ", ".join(sorted(missing))
            if len(missing) <= MAX_NAMED
            else f"{len(missing)} settings"
        )
        return config, Finding(
            "warn",
            "Config",
            f"using defaults for {named}; `vommit setup` asks about them",
        )
    return config, Finding("ok", "Config", "complete")


def _switched_off(config: Config, path: str) -> bool:
    """
    Whether `path` sits in a section that is disabled, so its settings never
    apply. `setup` writes only `enabled = false` for one, which is complete.
    """
    section, _, rest = path.partition(".")
    return bool(rest) and getattr(getattr(config, section), "enabled", True) is False


def _git(config: Config) -> Finding:
    git = config.active_git
    if git is None:
        return Finding("info", "Git", "disabled: no commit, tag or push")
    tag = git.tag_format or "no tag"
    return Finding(
        "info", "Git", f"branch {git.branch}, remote {git.origin}, tag {tag}"
    )


def _credential(project: VersionSource, config: Config, store: TokenStore) -> Finding:
    label = f"{registry(project)} credential"

    if isinstance(project, CrateProject):
        if source := cargo.token_source():
            return Finding("ok", label, f"from {source}")
        return Finding("warn", label, "none found; run `vommit authenticate`")

    if isinstance(project, NpmProject):
        if npm.has_auth_token():
            return Finding("ok", label, f"in {npm.NPMRC}")
        return Finding("warn", label, "none found; run `vommit authenticate`")

    pypi = config.active_pypi
    if pypi is not None and not pypi.use_keyring:
        return Finding("info", label, "asked for on every release")
    try:
        found = store.available()
    except VommitError as error:
        return Finding("warn", label, str(error))
    if found:
        source = "the environment" if found[0] == ENVIRONMENT else f"the {found[0]}"
        return Finding("ok", label, f"from {source}")
    return Finding(
        "warn", label, "none found; a release asks, or run `vommit authenticate`"
    )
