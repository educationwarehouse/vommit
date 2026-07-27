import json
import shlex
import typing as t
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from .commits import VersionBump
from .errors import VommitError
from .shell import Runner

PRERELEASE_BUMP = "rc"


@dataclass(frozen=True)
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
        project = tomlkit.parse(self.pyproject.read_text()).get("project")
        version = project.get("version") if isinstance(project, dict) else None
        return str(version) if version is not None else None

    def preview_bump(self, level: VersionBump, prerelease: bool = False) -> str:
        return self._version(self._bump_args(level, prerelease), dry_run=True)

    def preview_set(self, version: str) -> str:
        return self._version([version], dry_run=True)

    def apply_bump(self, level: VersionBump, prerelease: bool = False) -> str:
        return self._version(self._bump_args(level, prerelease), dry_run=False)

    def apply_set(self, version: str) -> str:
        return self._version([version], dry_run=False)

    @staticmethod
    def _bump_args(level: VersionBump, prerelease: bool) -> list[str]:
        args = ["--bump", level]
        if prerelease:
            args += ["--bump", PRERELEASE_BUMP]
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
