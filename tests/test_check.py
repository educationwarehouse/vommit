"""
`vommit check`: what vommit makes of a project, reported without changing it.
"""

import json
from pathlib import Path

import pytest

from src.vommit import cargo, check, npm
from src.vommit.auth import TOKEN_VAR, TokenStore
from src.vommit.config import Config
from src.vommit.shell import LocalRunner
from src.vommit.versioning import (
    CargoProject,
    CrateProject,
    NpmProject,
    UvProject,
)

from .conftest import BrokenKeyring, FakeKeyring

PYTHON = '[project]\nname = "pkg"\nversion = "1.2.3"\n'
CRATE = '[package]\nname = "crate"\nversion = "0.4.0"\n'


@pytest.fixture(autouse=True)
def no_ambient_credentials(tmp_path, monkeypatch) -> None:
    """
    Nothing from the machine running the tests: no token in the environment,
    and a `CARGO_HOME` with nothing stored in it.
    """
    monkeypatch.delenv(TOKEN_VAR, raising=False)
    monkeypatch.delenv(cargo.TOKEN_VAR, raising=False)
    home = tmp_path / "cargo-home"
    home.mkdir()
    monkeypatch.setenv("CARGO_HOME", str(home))


def configured(root: Path, body: str = PYTHON, complete: bool = True) -> Path:
    """
    A project whose `[tool.vommit]` either answers every setting or one.
    """
    pyproject = root / "pyproject.toml"
    pyproject.write_text(body)
    if complete:
        Config.default().write_to_pyproject(pyproject)
    else:
        with pyproject.open("a") as file:
            file.write("\n[tool.vommit]\nconfirm = true\n")
    return root


def run(root: Path, store: TokenStore | None = None) -> check.CheckReport:
    return check.run_check(root, LocalRunner(), store or TokenStore(FakeKeyring()))


def finding(report: check.CheckReport, label: str) -> check.Finding:
    return next(found for found in report.findings if found.label == label)


def test_a_complete_python_project(tmp_path):
    report = run(configured(tmp_path))

    assert report.ok is True
    assert finding(report, "Project").detail == "Python (uv)"
    assert finding(report, "Version").detail == "1.2.3, from pyproject.toml"
    assert finding(report, "Config").status == "ok"
    assert (
        finding(report, "Git").detail == "branch <head>, remote origin, tag v{version}"
    )
    assert finding(report, "Release runs").detail == (
        "clean: rm -rf ./dist; build: uv build; publish: uv publish"
    )


def test_check_writes_nothing(tmp_path):
    root = configured(tmp_path, complete=False)
    before = (root / "pyproject.toml").read_text()

    run(root)

    assert (root / "pyproject.toml").read_text() == before
    assert sorted(path.name for path in root.iterdir()) == [
        "cargo-home",
        "pyproject.toml",
    ]


@pytest.mark.parametrize(
    "project, kind, registry",
    [
        (UvProject, "Python (uv)", "PyPI"),
        (CargoProject, "Python, built from a crate by maturin", "PyPI"),
        (CrateProject, "Rust crate", "crates.io"),
        (NpmProject, "npm package", "npm"),
    ],
)
def test_each_kind_of_project(tmp_path, project, kind, registry):
    source = project(runner=LocalRunner(), root=tmp_path)

    assert check.project_kind(source) == kind
    assert check.registry(source) == registry


def test_a_workspace_is_named_as_one(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = []\n\n[workspace.package]\nversion = "0.1.0"\n'
    )
    source = CrateProject(runner=LocalRunner(), root=tmp_path)
    assert check.project_kind(source) == "Rust workspace"


def test_a_project_vommit_cannot_place_fails_and_stops_there(tmp_path):
    (tmp_path / "Cargo.toml").write_text(CRATE)
    (tmp_path / "package.json").write_text(json.dumps({"version": "1.0.0"}))

    report = run(tmp_path)

    assert report.ok is False
    assert [found.label for found in report.findings] == ["Project"]
    assert "Both package.json and Cargo.toml" in report.findings[0].detail


def test_no_version_anywhere_fails(tmp_path):
    report = run(configured(tmp_path, body=""))

    assert report.ok is False
    assert finding(report, "Version").detail == "none found in pyproject.toml"


def test_no_vommit_config_fails_and_stops_there(tmp_path):
    (tmp_path / "pyproject.toml").write_text(PYTHON)

    report = run(tmp_path)

    assert report.ok is False
    assert finding(report, "Config").status == "fail"
    assert "vommit setup" in finding(report, "Config").detail
    assert "Release runs" not in [found.label for found in report.findings]


def test_a_config_that_does_not_load_fails(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        PYTHON + '\n[tool.vommit]\nconfirm = "maybe"\n'
    )

    report = run(tmp_path)

    assert report.ok is False
    assert finding(report, "Config").detail.startswith("could not be loaded")


def test_a_few_unconfigured_settings_are_named(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "MAX_NAMED", 100)
    report = run(configured(tmp_path, complete=False))

    config = finding(report, "Config")
    assert config.status == "warn"
    assert "git.branch" in config.detail
    assert report.ok is True


def test_many_unconfigured_settings_are_counted(tmp_path):
    report = run(configured(tmp_path, complete=False))

    missing = len(Config.interactive_key_paths()) - 1
    assert f"using defaults for {missing} settings" in finding(report, "Config").detail


def test_git_switched_off(tmp_path):
    root = configured(tmp_path)
    config = Config.from_pyproject(root)
    config.git.enabled = False
    config.write_to_pyproject(root / "pyproject.toml")

    assert finding(run(root), "Git").detail == "disabled: no commit, tag or push"


def test_git_without_tags(tmp_path):
    root = configured(tmp_path)
    config = Config.from_pyproject(root)
    config.git.tag_format = ""
    config.write_to_pyproject(root / "pyproject.toml")

    assert finding(run(root), "Git").detail.endswith("tag no tag")


def test_a_release_with_no_commands_says_so(tmp_path):
    root = configured(tmp_path)
    config = Config.from_pyproject(root)
    for name in ("clean", "build", "publish", "post_publish"):
        setattr(config.commands, name, "")
    config.write_to_pyproject(root / "pyproject.toml")

    report = run(root)

    assert finding(report, "Release runs").status == "warn"
    # nothing publishes, so there is no credential to look for
    assert not any("credential" in found.label for found in report.findings)


def test_a_crate_with_and_without_a_token(tmp_path, monkeypatch):
    root = configured(tmp_path, body="")
    (root / "Cargo.toml").write_text(CRATE)

    assert finding(run(root), "crates.io credential").status == "warn"

    monkeypatch.setenv(cargo.TOKEN_VAR, "cio_secret")
    found = finding(run(root), "crates.io credential")
    assert (found.status, found.detail) == ("ok", f"from ${cargo.TOKEN_VAR}")


def test_an_npm_package_with_and_without_a_token(tmp_path, monkeypatch):
    root = configured(tmp_path, body="")
    (root / "package.json").write_text(json.dumps({"version": "1.0.0"}))

    monkeypatch.setattr(npm, "has_auth_token", lambda: False)
    assert finding(run(root), "npm credential").status == "warn"

    monkeypatch.setattr(npm, "has_auth_token", lambda: True)
    assert finding(run(root), "npm credential").detail == f"in {npm.NPMRC}"


def test_a_pypi_token_from_the_keyring(tmp_path):
    store = TokenStore(FakeKeyring({("vommit", "pypi"): "pypi-secret"}))
    found = finding(run(configured(tmp_path), store), "PyPI credential")
    assert (found.status, found.detail) == ("ok", "from the keyring")


def test_a_pypi_token_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_VAR, "pypi-secret")
    found = finding(run(configured(tmp_path)), "PyPI credential")
    assert found.detail == "from the environment"


def test_no_pypi_token_is_a_warning_not_a_failure(tmp_path):
    report = run(configured(tmp_path))

    assert finding(report, "PyPI credential").status == "warn"
    assert report.ok is True


def test_a_keyring_that_cannot_be_read_is_a_warning(tmp_path):
    report = run(configured(tmp_path), TokenStore(BrokenKeyring()))
    assert finding(report, "PyPI credential").status == "warn"


def test_a_token_asked_for_every_release(tmp_path):
    root = configured(tmp_path)
    config = Config.from_pyproject(root)
    config.pypi.use_keyring = False
    config.write_to_pyproject(root / "pyproject.toml")

    assert finding(run(root), "PyPI credential").detail == "asked for on every release"


def test_build_warnings_are_listed(tmp_path):
    root = configured(
        tmp_path,
        body=PYTHON + '\n[build-system]\nrequires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n',
    )

    report = run(root)

    assert finding(report, "Build check hatchling-backend").status == "warn"
    assert report.ok is True


def test_settings_in_a_switched_off_section_are_not_missing(tmp_path):
    """
    `setup` writes a disabled section as `enabled = false` alone, so the rest
    of it is never configured, and never needs to be.
    """
    root = configured(tmp_path)
    config = Config.from_pyproject(root)
    config.pypi.enabled = False
    pyproject = root / "pyproject.toml"
    pyproject.write_text(PYTHON)
    config.write_to_pyproject(pyproject)

    assert "use_keyring" not in pyproject.read_text()
    assert finding(run(root), "Config").status == "ok"
