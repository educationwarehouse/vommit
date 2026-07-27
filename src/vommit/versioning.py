import dataclasses as dc
import json
import shlex
import typing as t
from pathlib import Path

import tomlkit
from packaging.version import InvalidVersion, Version

from .commits import BUMP_PRIORITY, VersionBump
from .errors import VommitError
from .shell import Runner

PrereleaseToken = t.Literal["alpha", "beta", "rc"]
DEFAULT_PRERELEASE_TOKEN: PrereleaseToken = "rc"

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
    if level == "minor":
        return (major, minor + 1, 0)
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
    return "minor" if minor else "major"


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
    heading for -- `baseline` (the last released version) bumped by `level`,
    never lower than what the prerelease itself already claims -- and either
    promote that series or, if a bigger change has landed since, replace it.
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
class UvProject:
    """
    `uv version`, split into a read, a preview and an apply.

    Previewing first is what lets the whole release be validated before
    `pyproject.toml` is touched.
    """

    runner: Runner
    root: Path

    @property
    def pyproject(self) -> Path:
        return self.root / "pyproject.toml"

    @property
    def lockfile(self) -> Path:
        return self.root / "uv.lock"

    def current_version(self) -> str | None:
        """
        The version as written; None for projects that declare it dynamically.
        """
        if not self.pyproject.exists():
            return None
        return self.version_in(self.pyproject.read_text())

    @staticmethod
    def version_in(pyproject: str) -> str | None:
        """
        The declared version of any `pyproject.toml`, including an older revision.
        """
        project = tomlkit.parse(pyproject).get("project")
        version = project.get("version") if isinstance(project, dict) else None
        return str(version) if version is not None else None

    def preview(self, args: list[str]) -> str:
        """
        What `uv version <args>` would produce, without writing anything.
        """
        return self._version(args, dry_run=True)

    def apply(self, args: list[str]) -> str:
        return self._version(args, dry_run=False)

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
    ) -> str:
        return self.apply(self._bump_args(level, prerelease, token))

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

    def _version(self, args: list[str], dry_run: bool) -> str:
        # --frozen keeps a preview from writing a lockfile at all; a real bump
        # re-locks (the new version lands in uv.lock, which we commit) but has
        # no reason to rebuild the virtualenv.
        extra = ["--dry-run", "--frozen"] if dry_run else ["--no-sync"]
        command = shlex.join(
            [
                "uv",
                "--directory",
                str(self.root),
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
