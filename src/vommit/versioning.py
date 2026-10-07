import abc
import dataclasses as dc
import json
import shlex
import tempfile
import typing as t
from pathlib import Path

import tomlkit
from packaging.version import InvalidVersion, Version

from .commits import BUMP_PRIORITY, VersionBump
from .errors import VommitError
from .helpers import relative_path
from .shell import Runner

PrereleaseToken = t.Literal["alpha", "beta", "rc"]
DEFAULT_PRERELEASE_TOKEN: PrereleaseToken = "rc"

PYPROJECT = "pyproject.toml"
CARGO = "Cargo.toml"
PACKAGE_JSON = "package.json"

# what to indent `package.json` by when the file gives nothing to go on
DEFAULT_JSON_INDENT = 2

# The SemVer spelling of each PEP 440 prerelease marker. SemVer is what both
# Cargo and npm demand, so `1.2.3rc1` is a parse error in either; maturin reads
# the SemVer back out and normalises it to exactly the PEP 440 form these came
# from, which is what keeps the wheel's version equal to the one vommit tagged.
SEMVER_PRERELEASE: dict[str, str] = {"a": "alpha", "b": "beta", "rc": "rc"}

# `uv version --bump stable` drops a prerelease segment without touching the
# release numbers: 1.1.0rc2 -> 1.1.0.
STABLE_BUMP = "stable"

Release = tuple[int, int, int]


def parse_version(raw: str | None) -> Version | None:
    """
    None for anything that is not a PEP 440 version (including None itself).
    """
    if not raw:
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def is_prerelease(raw: str | None) -> bool:
    """
    Whether `raw` is anything other than a finished release.

    Dev releases count: they are no more publishable than an rc, so the
    changelog should treat them the same way.
    """
    parsed = parse_version(raw)
    return bool(parsed and (parsed.is_prerelease or parsed.is_devrelease))


def release_of(version: Version) -> Release:
    """
    The (major, minor, patch) triple, padding versions written with fewer parts.
    """
    parts = (*version.release, 0, 0)
    return t.cast(Release, parts[:3])


def bumped_release(release: Release, level: VersionBump) -> Release:
    major, minor, patch = release
    if level == "major":
        return (major + 1, 0, 0)
    elif level == "minor":
        return (major, minor + 1, 0)
    else:
        return (major, minor, patch + 1)


def implied_level(release: Release) -> VersionBump:
    """
    The largest bump `release` could have been the result of, read off its zeros.

    1.0.0 can only be a major bump, 1.2.0 a minor one, 1.2.3 a patch. Used to
    size up a prerelease when there is no released version to compare it against.
    """
    _, minor, patch = release
    if patch:
        return "patch"
    elif minor:
        return "minor"
    else:
        return "major"


def to_semver(version: str, manifest: str = CARGO) -> str:
    """
    `version` as plain SemVer, for a manifest that has to hold it.

    `Cargo.toml` and `package.json` want the same thing: PEP 440 in, SemVer
    out, `1.2.3rc1` spelled `1.2.3-rc.1`. `manifest` only names the file in
    the error messages.

    Only the shapes vommit produces convert: a release, optionally with an
    alpha/beta/rc prerelease. Post, dev, epoch and local segments have no
    SemVer spelling that reads back as the same version, and `--version` can
    ask for any of them, so they are refused before anything is written.
    """
    parsed = parse_version(version)
    if parsed is None:
        raise VommitError(f"{version!r} is not a PEP 440 version.")

    unsupported = [
        name
        for name, present in (
            ("an epoch", parsed.epoch),
            ("a post-release", parsed.post is not None),
            ("a dev-release", parsed.dev is not None),
            ("a local version", parsed.local is not None),
        )
        if present
    ]
    if unsupported:
        raise VommitError(
            f"{version} has {' and '.join(unsupported)}, which SemVer cannot "
            f"express; give {manifest} a version made of a release and at most "
            "an alpha, beta or rc prerelease."
        )

    major, minor, patch = release_of(parsed)
    semver = f"{major}.{minor}.{patch}"
    if parsed.pre:
        label, number = parsed.pre
        semver = f"{semver}-{SEMVER_PRERELEASE[label]}.{number}"

    # the conversion is only worth anything if it survives the trip back: the
    # wheel maturin builds is named after what *it* makes of this string.
    # compared as versions, not text: padding `1.2` out to three components is
    # not a change of version.
    if Version(semver) != parsed:  # pragma: no cover - backstop, not a path
        raise VommitError(
            f"{version} would be written to {manifest} as {semver}, which reads "
            f"back as {Version(semver)}; refusing to change the version."
        )
    return semver


def _series_covers(claimed: Release, level: VersionBump, baseline: str | None) -> bool:
    """
    Whether a prerelease claiming `claimed` is still big enough for `level`.

    With a released version to measure from this is exact: bump it by `level` and
    see whether the prerelease already reaches that far. Without one, the shape
    of `claimed` is the only evidence available.
    """
    baseline_version = parse_version(baseline)
    if baseline_version is None:
        return BUMP_PRIORITY[implied_level(claimed)] >= BUMP_PRIORITY[level]
    return claimed >= bumped_release(release_of(baseline_version), level)


def plan_bump(
    current: str | None,
    level: VersionBump,
    baseline: str | None = None,
    prerelease_token: str | None = None,
) -> list[str]:
    """
    The `uv version` arguments that land on the intended next version.

    uv derives everything from the version in `pyproject.toml`, which is the
    wrong reference point once that version is a prerelease: from 1.1.0rc1,
    `--bump minor` yields 1.2.0, skipping the 1.1.0 the rc series was built for.

    So when the current version is a prerelease we work out the release it was
    heading for: `baseline` (the last released version) bumped by `level`,
    never lower than what the prerelease itself already claims. That series is
    then either promoted or, if a bigger change has landed since, replaced.
    """
    token_args = [] if prerelease_token is None else ["--bump", prerelease_token]
    parsed = parse_version(current)

    if parsed is None or not (parsed.is_prerelease or parsed.is_devrelease):
        return ["--bump", level, *token_args]

    if not _series_covers(release_of(parsed), level, baseline):
        # a weightier change arrived than this series planned for; start over
        return ["--bump", level, *token_args]

    # the series stands: step it, or drop the prerelease segment to release it
    return token_args or ["--bump", STABLE_BUMP]


@dc.dataclass(frozen=True)
class VersionSource(abc.ABC):
    """
    The file a project's version lives in, split into a read, a preview and an
    apply.

    Previewing first is what lets the whole release be validated before that
    file is touched. Which file it is varies (`pyproject.toml` for a Python
    package, `Cargo.toml` for one whose wheel is built from a crate), but the
    arithmetic never does: every subclass hands the same `uv version` arguments
    to uv and writes back whatever uv makes of them, so two projects bumped by
    the same commits land on the same version.
    """

    runner: Runner
    root: Path

    # The file holding the version, relative to `root`.
    manifest_name: t.ClassVar[str]
    # The lockfile that records it a second time, relative to `root`, or None
    # for a backend that manages none (see `NpmProject`).
    lockfile_name: t.ClassVar[str | None]

    @property
    def manifest(self) -> Path:
        return self.root / self.manifest_name

    @property
    def lockfile(self) -> Path | None:
        return self.root / self.lockfile_name if self.lockfile_name else None

    def current_version(self) -> str | None:
        """
        The version as written; None when this file does not state one.
        """
        if not self.manifest.exists():
            return None
        return self.version_in(self.manifest.read_text())

    @abc.abstractmethod
    def version_in(self, manifest: str) -> str | None:
        """
        The declared version of any revision of the manifest, including an old one.
        """

    @abc.abstractmethod
    def preview(self, args: list[str]) -> str:
        """
        What `uv version <args>` would produce, without writing anything.
        """

    @abc.abstractmethod
    def apply(self, args: list[str], frozen: bool = False) -> str:
        """
        Write what `uv version <args>` produces, and return it.
        """

    def preview_bump(
        self,
        level: VersionBump,
        prerelease: bool = False,
        token: PrereleaseToken = DEFAULT_PRERELEASE_TOKEN,
    ) -> str:
        return self.preview(self._bump_args(level, prerelease, token))

    def preview_set(self, version: str) -> str:
        return self.preview([version])

    def apply_bump(
        self,
        level: VersionBump,
        prerelease: bool = False,
        token: PrereleaseToken = DEFAULT_PRERELEASE_TOKEN,
        frozen: bool = False,
    ) -> str:
        return self.apply(self._bump_args(level, prerelease, token), frozen=frozen)

    def apply_set(self, version: str) -> str:
        return self.apply([version])

    @staticmethod
    def _bump_args(
        level: VersionBump,
        prerelease: bool,
        token: PrereleaseToken = DEFAULT_PRERELEASE_TOKEN,
    ) -> list[str]:
        args = ["--bump", level]
        if prerelease:
            args += ["--bump", token]
        return args

    def _uv_version(self, directory: Path, args: list[str], extra: list[str]) -> str:
        command = shlex.join(
            [
                "uv",
                "--directory",
                str(directory),
                "version",
                *args,
                *extra,
                "--output-format",
                "json",
            ]
        )

        result = self.runner.run(command)
        if not result.ok:
            raise VommitError(f"`uv version` failed: {result.error}")

        try:
            payload: dict[str, t.Any] = json.loads(result.stdout)
            return str(payload["version"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise VommitError(
                f"Could not read a version from `uv version`: {result.out}"
            ) from exc


@dc.dataclass(frozen=True)
class UvProject(VersionSource):
    """
    A project whose version is the static `[project].version` uv maintains.
    """

    manifest_name: t.ClassVar[str] = PYPROJECT
    lockfile_name: t.ClassVar[str | None] = "uv.lock"

    def version_in(self, manifest: str) -> str | None:
        project = tomlkit.parse(manifest).get("project")
        version = project.get("version") if isinstance(project, dict) else None
        return str(version) if version is not None else None

    def preview(self, args: list[str]) -> str:
        return self._version(args, dry_run=True)

    def apply(self, args: list[str], frozen: bool = False) -> str:
        return self._version(args, dry_run=False, frozen=frozen)

    def _version(self, args: list[str], dry_run: bool, frozen: bool = False) -> str:
        # --frozen keeps a preview from writing a lockfile at all. A real bump
        # re-locks by default, but projects that ignore their lockfile should
        # not get one merely from changing their version.
        if dry_run:
            extra = ["--dry-run", "--frozen"]
        elif frozen:
            extra = ["--frozen"]
        else:
            extra = ["--no-sync"]
        return self._uv_version(self.root, args, extra)


@dc.dataclass(frozen=True)
class SemVerProject(VersionSource):
    """
    A project whose manifest holds plain SemVer, not PEP 440.

    The arithmetic stays uv's, so every backend lands on the same version from
    the same commits: uv runs against a throwaway `pyproject.toml` holding
    only the current version, and `to_semver` converts the answer back. A
    subclass supplies the manifest-specific parts: `version_in`,
    `_write_version` and `missing_version`.
    """

    # How this manifest spells the version field, for the error when it
    # states none.
    missing_version: t.ClassVar[str]

    @abc.abstractmethod
    def _write_version(self, semver: str) -> None:
        """
        Put `semver` in the manifest, leaving everything else as it was found.
        """

    def _after_write(self, frozen: bool) -> None:
        """
        Whatever else a write implies; nothing, for a backend with no lockfile.
        """

    def preview(self, args: list[str]) -> str:
        current = self._require_current()
        with tempfile.TemporaryDirectory() as scratch:
            probe = Path(scratch)
            (probe / PYPROJECT).write_text(
                f'[project]\nname = "vommit-version-probe"\nversion = "{current}"\n'
            )
            # no lockfile to keep in step and nothing to sync: the probe exists
            # only to be read back out
            return self._uv_version(probe, args, ["--dry-run", "--frozen"])

    def apply(self, args: list[str], frozen: bool = False) -> str:
        version = self.preview(args)
        # converted before the write, so a version the manifest cannot hold
        # stops the bump instead of landing half-applied
        self._write_version(to_semver(version, self.manifest_name))
        self._after_write(frozen)
        return version

    def _require_current(self) -> str:
        current = self.current_version()
        if current:
            return current
        raise VommitError(
            f"No `{self.missing_version}` in "
            f"{relative_path(self.manifest, self.root)}, so there is no version "
            "to bump."
        )


@dc.dataclass(frozen=True)
class CargoProject(SemVerProject):
    """
    A project whose version is `[package].version` in `Cargo.toml`.

    maturin builds the wheel from the crate and takes its version from there, so
    `pyproject.toml` says `dynamic = ["version"]` and has nothing for
    `uv version` to bump; uv refuses the project outright.
    """

    manifest_name: t.ClassVar[str] = CARGO
    lockfile_name: t.ClassVar[str | None] = "Cargo.lock"
    missing_version: t.ClassVar[str] = "[package].version"
    # maturin's `[tool.maturin].manifest-path`, when the crate is not at root.
    manifest_path: Path | None = None

    @property
    def manifest(self) -> Path:
        return self.manifest_path or super().manifest

    @property
    def lockfile(self) -> Path:
        if self.manifest_path is None:
            # the base returns None only for a backend with no lockfile_name,
            # and this class has one
            return t.cast(Path, super().lockfile)
        else:
            return self.manifest_path.parent / t.cast(str, self.lockfile_name)

    def version_in(self, manifest: str) -> str | None:
        package = tomlkit.parse(manifest).get("package")
        version = package.get("version") if isinstance(package, dict) else None
        # Cargo's spelling of a prerelease is a legal PEP 440 one, so parsing is
        # also the conversion: `1.2.3-rc.1` reads back as `1.2.3rc1`, which is
        # the version maturin will put on the wheel.
        parsed = parse_version(str(version)) if version is not None else None
        return str(parsed) if parsed else None

    def _write_version(self, semver: str) -> None:
        document = tomlkit.parse(self.manifest.read_text())
        package = document.get("package")
        if not isinstance(package, dict):
            raise VommitError(f"[package] must be a TOML table in {CARGO}.")
        package["version"] = semver
        self.manifest.write_text(tomlkit.dumps(document))

    def _after_write(self, frozen: bool) -> None:
        if not frozen:
            self._relock()

    def _relock(self) -> None:
        """
        Carry the new version into `Cargo.lock`, the way `uv version` relocks.

        `--workspace` keeps this to what the manifest already pins: a bump
        moves no dependency versions. Online, because a dependency pinned to a
        git checkout cannot resolve offline.
        """
        if not self.lockfile.exists():
            return
        command = shlex.join(
            [
                "cargo",
                "update",
                "--workspace",
                "--manifest-path",
                str(self.manifest),
            ]
        )
        result = self.runner.run(command)
        if not result.ok:
            raise VommitError(
                f"The version in {CARGO} was updated, but `cargo update` could "
                f"not carry it into {self.lockfile_name}: {result.error}"
            )


@dc.dataclass(frozen=True)
class NpmProject(SemVerProject):
    """
    A project whose version is `.version` in `package.json`.

    No lockfile: npm, pnpm, yarn and bun each keep a different one and
    disagree on its shape, and the next install fixes a stale one anyway.
    `lockfile_name` is None so nothing downstream stages or relocks a file
    this never wrote.
    """

    manifest_name: t.ClassVar[str] = PACKAGE_JSON
    lockfile_name: t.ClassVar[str | None] = None
    missing_version: t.ClassVar[str] = ".version"

    def version_in(self, manifest: str) -> str | None:
        version = self._declared_version(manifest)
        parsed = parse_version(version) if version is not None else None
        return str(parsed) if parsed else None

    def unsupported_version(self) -> str | None:
        """
        The manifest's `.version` when npm accepts it but this cannot bump it,
        None otherwise.

        `version_in` answers None both for a prerelease PEP 440 cannot read
        (`1.0.0-alpha.beta`) and for no version at all, and those two want
        different messages, so the distinction is drawn here.
        """
        if not self.manifest.exists():
            return None
        version = self._declared_version(self.manifest.read_text())
        if version is None or parse_version(version):
            return None
        return version

    def _declared_version(self, manifest: str) -> str | None:
        try:
            data = json.loads(manifest)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        version = data.get("version")
        return str(version) if version is not None else None

    def _write_version(self, semver: str) -> None:
        # read as text first: only the version changes, so reformatting the
        # rest would put the whole manifest in the release commit's diff
        raw = self.manifest.read_text()
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise VommitError(f"{PACKAGE_JSON} must contain a JSON object.")
        document["version"] = semver
        text = json.dumps(document, indent=_json_indent(raw))
        self.manifest.write_text(text + "\n" if raw.endswith("\n") else text)


def _json_indent(raw: str) -> int | str:
    """
    The indentation `raw` uses, in the form `json.dumps` takes it.

    Read off the first indented line: `json.loads` throws formatting away.
    """
    for line in raw.splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            return line[: len(line) - len(stripped)]
    return DEFAULT_JSON_INDENT


def version_source(runner: Runner, root: Path) -> VersionSource:
    """
    Where this project keeps the version vommit is asked to move.

    `Cargo.toml` wins only for the one shape that leaves uv nothing to work
    with: a maturin build whose `pyproject.toml` declares the version dynamic.
    A real `[project].version` wins next: that is still what gets built, for a
    Python package vendoring a crate or carrying a frontend's `package.json`.
    `package.json` gets a look in only once neither states a version, which is
    what resolves a project whose `pyproject.toml` is a `[tool.vommit]` stanza
    and nothing else.
    """
    pyproject = root / PYPROJECT
    cargo = CargoProject(
        runner=runner,
        root=root,
        manifest_path=_maturin_manifest(pyproject),
    )
    if _is_dynamic_maturin(pyproject) and cargo.current_version():
        return cargo

    uv_project = UvProject(runner=runner, root=root)
    if uv_project.current_version() is not None:
        return uv_project

    npm_project = NpmProject(runner=runner, root=root)
    if npm_project.current_version() is not None:
        return npm_project
    if unsupported := npm_project.unsupported_version():
        # falling through reports "no version in pyproject.toml", the wrong
        # file for a project that plainly states one
        raise VommitError(
            f"{relative_path(npm_project.manifest, root)} is at "
            f"{unsupported!r}, which vommit cannot bump: it releases versions "
            "made of a release number and at most an alpha, beta or rc "
            "prerelease."
        )

    return uv_project


def _maturin_manifest(pyproject: Path) -> Path | None:
    """
    The crate maturin is configured to build, if it is not the root Cargo.toml.
    """
    if not pyproject.exists():
        return None
    tool = tomlkit.parse(pyproject.read_text()).get("tool")
    maturin = tool.get("maturin") if isinstance(tool, dict) else None
    configured = maturin.get("manifest-path") if isinstance(maturin, dict) else None
    return pyproject.parent / str(configured) if configured else None


def _is_dynamic_maturin(pyproject: Path) -> bool:
    if not pyproject.exists():
        return False
    document = tomlkit.parse(pyproject.read_text())

    project = document.get("project")
    dynamic = project.get("dynamic", []) if isinstance(project, dict) else []
    if "version" not in [str(entry) for entry in dynamic]:
        return False

    build_system = document.get("build-system")
    backend = (
        str(build_system.get("build-backend", ""))
        if isinstance(build_system, dict)
        else ""
    )
    return backend.split(".")[0] == "maturin"
