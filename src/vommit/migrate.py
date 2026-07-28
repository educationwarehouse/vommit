import csv
import dataclasses as dc
import difflib
import re
import typing as t
from pathlib import Path, PurePosixPath

import tomlkit

from .commits import BREAKING, VersionBump
from .config import DERIVE_BRANCH, ChangelogConfig, Config
from .errors import VommitError
from .helpers import read_toml, throw
from .versioning import PrereleaseToken, parse_version

PSR_KEY = "tool.semantic_release"

UnsupportedPolicy = t.Literal["warn", "error", "skip"]
UNSUPPORTED_POLICIES: tuple[UnsupportedPolicy, ...] = t.get_args(UnsupportedPolicy)

ANGULAR_PARSER = "semantic_release.history.angular_parser"

# where vommit keeps the version; v7 could be pointed anywhere
PROJECT_VERSION_KEY = "pyproject.toml:project.version"

# v7's own `parser_angular_default_level_bump` vocabulary
LEVEL_BUMPS: dict[str, VersionBump | None] = {
    "no-release": None,
    "patch": "patch",
    "minor": "minor",
    "major": "major",
}

# v7 named changelog sections after the *long* type names its angular parser
# emitted (`history/parser_angular.py:18`); only these three differ from the
# commit type itself. `breaking` is v7's name for what vommit calls `break`.
SECTION_TYPES = {
    "feature": "feat",
    "documentation": "docs",
    "performance": "perf",
    "breaking": BREAKING,
}

# v7's catch-all bucket for types nobody configured; vommit has no equivalent
# and a level named `other` would never match a commit.
CATCH_ALL_SECTION = "other"

LEVEL_TITLES = {
    BREAKING: "Breaking Change{s}",
    "feat": "Feature{s}",
    "fix": "Fix{es}",
    "perf": "Performance",
    "docs": "Documentation",
    "refactor": "Refactor{s}",
    "test": "Test{s}",
    "build": "Build{s}",
    "chore": "Chore{s}",
    "style": "Style{s}",
    "ci": "CI",
}

# PEP 440 spells these a/b/rc; `uv version --bump` wants the long names, and v7
# accepted anything at all.
PRERELEASE_TOKENS: dict[str, PrereleaseToken] = {
    "a": "alpha",
    "alpha": "alpha",
    "b": "beta",
    "beta": "beta",
    "c": "rc",
    "rc": "rc",
}

RE_CHANGE_TYPE = re.compile(r"[a-z][a-z0-9-]*$")

# leading distribution name of a requirement string, before any extra,
# specifier or marker
RE_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+")

# the runs of separators PEP 503 collapses when comparing distribution names
RE_NAME_SEPARATORS = re.compile(r"[-_.]+")

PSR_DISTRIBUTION = "python-semantic-release"

METADATA_IMPORT = "from importlib.metadata import version"

Backend = t.Literal["hatchling", "setuptools", "uv_build"]

BUILD_BACKENDS: dict[str, Backend] = {
    "hatchling.build": "hatchling",
    "setuptools.build_meta": "setuptools",
    "setuptools.build_meta:__legacy__": "setuptools",
    "uv_build": "uv_build",
}

# Where each backend declares the dynamic version it reads. uv_build is absent
# on purpose: it has never supported dynamic versions, which is why v7 sat
# badly with it -- and why it is the shape the other two get transformed into.
HOOK_KEYS: dict[str, str] = {
    "hatchling": "tool.hatch.version",
    "setuptools": "tool.setuptools.dynamic.version",
}

# The defaults v7 applied to anything the project left unset (`defaults.cfg`).
# They matter: a v7 project's release history was produced with these, so
# translating only the keys someone wrote down changes behaviour on exactly the
# projects that configured the least.
#
# Two deliberate omissions:
# - `build_command`, whose v7 default is `python setup.py sdist bdist_wheel`.
#   That is legacy tooling rather than a release decision, and vommit's own
#   `uv build` supersedes it.
# - the emoji half of `changelog_sections`, which exists for the emoji parser
#   and translates to nothing. Dropping it here keeps the default translation
#   free of warnings about sections nobody asked for.
PSR_DEFAULTS: dict[str, t.Any] = {
    "branch": "master",
    "changelog_file": "CHANGELOG.md",
    "changelog_placeholder": "<!--next-version-placeholder-->",
    "changelog_sections": "feature,fix,breaking,documentation,performance,Other",
    "commit_parser": ANGULAR_PARSER,
    "commit_subject": "{version}",
    "dist_path": "dist",
    "parser_angular_allowed_types": "build,chore,ci,docs,feat,fix,perf,style,refactor,test",
    "parser_angular_default_level_bump": "no-release",
    "parser_angular_minor_types": "feat",
    "parser_angular_patch_types": "fix,perf",
    "prerelease_tag": "beta",
    "remove_dist": True,
    "tag_commit": True,
    "tag_format": "v{version}",
    "upload_to_pypi": True,
    "upload_to_repository": True,
}

# Read during translation, so never reported as unknown.
HANDLED_KEYS = frozenset(PSR_DEFAULTS) | {
    "build_command",
    "commit_message",
    "major_on_zero",
    "patch_without_tag",
    "version_pattern",
    "version_source",
    "version_toml",
    "version_variable",
}

# Recognised, but nothing on the vommit side answers to them.
UNSUPPORTED_KEYS: dict[str, str] = {
    "changelog_capitalize": "vommit takes commit descriptions as written",
    "changelog_components": "the changelog is rendered by vommit itself",
    "changelog_scope": "scopes are always shown",
    "check_build_status": "vommit does not poll a CI provider",
    # todo: let a project set the release commit author, and map this onto it
    "commit_author": "release commits carry your own git identity",
    "commit_version_number": "vommit always commits the version bump",
    "dist_glob_patterns": "publishing is a plain shell command (commands.publish)",
    "fix_tag": "emoji parser setting",
    "gitea_token_var": "vommit does not talk to a forge",
    "github_token_var": "vommit does not talk to a forge",
    "gitlab_token_var": "vommit does not talk to a forge",
    "hvcs": "vommit does not talk to a forge",
    "hvcs_api_domain": "vommit does not talk to a forge",
    "hvcs_domain": "vommit does not talk to a forge",
    "ignore_token_for_push": "pushing uses your own git credentials",
    "include_additional_files": "the release commit picks up whatever changed",
    "major_emoji": "emoji parser setting",
    "minor_emoji": "emoji parser setting",
    "minor_tag": "emoji parser setting",
    "patch_emoji": "emoji parser setting",
    "pre_commit_command": "no hook between the bump and the commit (yet)",
    "pypi_pass_var": "PyPI credentials come from keyring or the environment",
    "pypi_token_var": "PyPI credentials come from keyring or the environment",
    "pypi_user_var": "PyPI credentials come from keyring or the environment",
    "repository": "publishing is a plain shell command (commands.publish)",
    "repository_pass_var": "PyPI credentials come from keyring or the environment",
    "repository_url": "publishing is a plain shell command (commands.publish)",
    "repository_url_var": "publishing is a plain shell command (commands.publish)",
    "repository_user_var": "PyPI credentials come from keyring or the environment",
    "upload_to_pypi_glob_patterns": "publishing is a plain shell command (commands.publish)",
    "upload_to_release": "vommit does not create forge releases",
    "use_only_cwd_commits": "vommit reads the whole history of the branch",
    "use_textual_changelog_sections": "emoji parser setting",
}

KNOWN_KEYS = HANDLED_KEYS | frozenset(UNSUPPORTED_KEYS)


@dc.dataclass(frozen=True)
class VersionFile:
    """
    A `version_variable` entry: a file v7 wrote the version into.
    """

    path: str
    variable: str

    @classmethod
    def parse(cls, entry: str) -> t.Self | None:
        """
        None for an entry that is not `path:variable` (v7 would have crashed on it).
        """
        path, separator, variable = entry.partition(":")
        if not separator or not path.strip() or not variable.strip():
            return None
        return cls(path=path.strip(), variable=variable.strip())

    @property
    def pattern(self) -> re.Pattern[str]:
        """
        The assignment v7 rewrote, on v7's own terms (`history/__init__.py:75`).
        """
        return re.compile(
            rf"^(?P<prefix>[ \t]*{re.escape(self.variable)}[ \t]*[:=][ \t]*)"
            rf"(?P<quote>[\"'])(?P<version>[^\"']*)(?P=quote)",
            flags=re.MULTILINE,
        )

    def read(self, root: Path) -> str | None:
        """
        The version currently written in this file, if it is there to be read.
        """
        target = root / self.path
        if not target.exists():
            return None
        match = self.pattern.search(target.read_text())
        return match.group("version") if match else None


@dc.dataclass(frozen=True)
class Note:
    key: str
    detail: str

    def __str__(self) -> str:
        return f"{self.key}: {self.detail}"


@dc.dataclass(frozen=True)
class VersionTransform:
    """
    The `[project]` edits that give vommit a version it can bump.

    Two shapes reach this: a dynamic version to undo (`backend` and `hook_key`
    set), and a `[project]` that never declared a version at all -- legal under
    v7, which kept it in the source file, and unbuildable without one now.
    """

    version: str
    # where `version` was read, so the confirmation can say so
    source: str
    # both None when there was no dynamic version to undo, only one to add
    backend: str | None = None
    hook_key: str | None = None

    @property
    def steps(self) -> list[str]:
        steps = [f'set [project].version = "{self.version}" (read from {self.source})']
        if self.hook_key:
            steps += [
                'remove "version" from [project].dynamic',
                f"remove [{self.hook_key}]",
            ]
        return steps


@dc.dataclass(frozen=True)
class VersionPlan:
    """
    The version half of a migration, planned before anything is written.

    The freeze and the rewrites are one change: pointing `__version__` at the
    installed metadata while the version lives nowhere else leaves the project
    with no version at all, which is why `plan_version_migration` refuses that
    combination rather than reporting it afterwards.
    """

    transform: VersionTransform | None = None
    rewrites: tuple[VersionFile, ...] = ()
    warnings: tuple[Note, ...] = ()

    @property
    def empty(self) -> bool:
        return self.transform is None and not self.rewrites

    @property
    def steps(self) -> list[str]:
        steps = list(self.transform.steps) if self.transform else []
        steps += [
            f"rewrite {version_file.path} to "
            f"`{version_file.variable} = version(__package__)`"
            for version_file in self.rewrites
        ]
        return steps

    @property
    def question(self) -> str:
        if self.transform is None:
            return "Point the version file at the installed metadata?"
        if self.transform.hook_key:
            return "This project declares a dynamic version, which cannot be bumped. Fix it?"
        return "This project declares no [project].version, which vommit needs to bump. Add it?"


@dc.dataclass(frozen=True)
class RewriteResult:
    changed: list[Path] = dc.field(default_factory=list)
    skipped: list[Note] = dc.field(default_factory=list)


@dc.dataclass(frozen=True)
class MigrationReport:
    """
    What the translation did, in three buckets the caller renders differently.

    `mapped` carries settings that came across, `lossy` settings that came
    across differently or not at all, `unsupported` keys vommit has no answer to.
    """

    mapped: list[Note] = dc.field(default_factory=list)
    lossy: list[Note] = dc.field(default_factory=list)
    unsupported: list[Note] = dc.field(default_factory=list)

    @property
    def warnings(self) -> list[Note]:
        return [*self.lossy, *self.unsupported]

    @property
    def clean(self) -> bool:
        return not self.warnings


@dc.dataclass(frozen=True)
class Migration:
    config: Config
    report: MigrationReport
    # dotted leaf paths that were filled in, for `Config.interactive(present_paths=...)`
    key_paths: set[str]
    version_files: list[VersionFile]


@dc.dataclass(frozen=True)
class Raw:
    """
    A v7 config as v7 itself saw it: the defaults, with the project on top.

    `explicit` is what the project actually wrote down. Warnings key off that,
    not off `values`: nobody needs to hear that they left `hvcs` at `github`.
    """

    values: dict[str, t.Any]
    explicit: frozenset[str]

    def get(self, key: str) -> t.Any:
        return self.values.get(key)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit


def read_psr(pyproject: Path) -> Raw | None:
    """
    The `[tool.semantic_release]` table, or None if the project has none.

    pyproject is the only source read. v7 also merged `setup.cfg` in, so a
    project that kept its config there is not migrated and not warned about;
    move those keys into pyproject first.
    """
    if not pyproject.exists():
        return None

    document = read_toml(pyproject)
    tool = document.get("tool")
    table = tool.get("semantic_release") if isinstance(tool, dict) else None
    if not isinstance(table, dict):
        return None

    explicit = {str(key): _plain(value) for key, value in table.items()}
    return Raw(values=PSR_DEFAULTS | explicit, explicit=frozenset(explicit))


def parse_unsupported_policy(value: str) -> UnsupportedPolicy:
    """
    `--on-unsupported`, checked rather than trusted.

    The option exists to make a migration stricter, so an unrecognised spelling
    has to fail: silently falling back to `warn` hands back the laxest
    behaviour to whoever just asked for the strictest.
    """
    if value not in UNSUPPORTED_POLICIES:
        raise VommitError(
            f"--on-unsupported must be one of {', '.join(UNSUPPORTED_POLICIES)}; "
            f"got {value!r}."
        )
    return value


def project_name(pyproject: Path) -> str | None:
    """
    The distribution name in `[project]`, if the project declares one.
    """
    if not pyproject.exists():
        return None
    project = read_toml(pyproject).get("project")
    name = project.get("name") if isinstance(project, dict) else None
    return str(name) if name else None


def _plain(value: t.Any) -> t.Any:
    """
    Strip tomlkit's wrapper types, so the translation compares plain Python.
    """
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return str(value)
    return value


def split_fields(value: t.Any) -> list[str]:
    """
    v7's multi-value keys: a list, or one comma-separated string with CSV
    escaping for literal commas (`history/__init__.py:452`).
    """
    if not value:
        return []
    if isinstance(value, list):
        items = [str(item) for item in value]
    else:
        items = next(csv.reader([str(value)]))
    return [item.strip() for item in items if item.strip()]


@dc.dataclass
class _Translation:
    """
    Accumulator for `translate`: the config being filled in, plus the notes.
    """

    raw: Raw
    config: Config
    mapped: list[Note] = dc.field(default_factory=list)
    lossy: list[Note] = dc.field(default_factory=list)
    unsupported: list[Note] = dc.field(default_factory=list)
    key_paths: set[str] = dc.field(default_factory=set)
    version_files: list[VersionFile] = dc.field(default_factory=list)

    def get(self, key: str) -> t.Any:
        return self.raw.get(key)

    def is_explicit(self, key: str) -> bool:
        return self.raw.is_explicit(key)

    @property
    def changelog(self) -> ChangelogConfig:
        """
        The section is optional on a `Config` read from disk, never on a fresh one.
        """
        return self.config.changelog or throw(
            VommitError("no changelog section to migrate into")
        )

    def assign(self, key: str, path: str, value: t.Any, detail: str = "") -> None:
        """
        Write `value` at a dotted `path`, and record that the path is now answered.
        """
        section, _, field = path.rpartition(".")
        target = getattr(self.config, section) if section else self.config
        setattr(target, field, value)
        self.key_paths.add(path)
        self.mapped.append(Note(key=key, detail=detail or f"{path} = {value!r}"))

    def note_lossy(self, key: str, detail: str) -> None:
        self.lossy.append(Note(key=key, detail=detail))

    def note_unsupported(self, key: str, detail: str) -> None:
        self.unsupported.append(Note(key=key, detail=detail))

    def finish(self) -> Migration:
        return Migration(
            config=self.config,
            report=MigrationReport(
                mapped=self.mapped,
                lossy=self.lossy,
                unsupported=self.unsupported,
            ),
            key_paths=self.key_paths,
            version_files=self.version_files,
        )


def translate(raw: Raw) -> Migration:
    """
    A v7 config as a vommit config, plus an account of what did not survive.

    Raises `VommitError` for the shapes vommit cannot release at all, before
    anything is written or even reported: a config that looks migrated but can
    never bump is worse than no config.
    """
    _refuse_unmigratable(raw)

    work = _Translation(raw=raw, config=Config.default())
    _translate_git(work)
    _translate_changelog(work)
    _translate_commands(work)
    _translate_pypi(work)
    _translate_bumps(work)
    _translate_prerelease(work)
    _translate_version_files(work)
    _translate_lossy(work)
    _collect_unsupported(work)
    return work.finish()


def _refuse_unmigratable(raw: Raw) -> None:
    parser = str(raw.get("commit_parser"))
    if parser != ANGULAR_PARSER:
        raise VommitError(
            f"commit_parser is {parser}; vommit only reads conventional commits. "
            f"Migration would silently change which commits count as a release."
        )

    if raw.is_explicit("version_pattern"):
        raise VommitError(
            "version_pattern points the version at an arbitrary regex; vommit "
            "reads it from [project].version and has nothing to rewrite it to."
        )

    for entry in split_fields(raw.get("version_toml")):
        if entry.strip() != PROJECT_VERSION_KEY:
            raise VommitError(
                f"version_toml is {entry}; vommit reads the version from "
                f"[project].version only. Move it there first, then migrate."
            )


def _translate_git(work: _Translation) -> None:
    _translate_branch(work)
    work.assign("commit_subject", "git.commit_format", str(work.get("commit_subject")))

    if work.get("tag_commit"):
        work.assign("tag_format", "git.tag_format", str(work.get("tag_format")))
    else:
        work.assign(
            "tag_commit",
            "git.tag_format",
            None,
            detail="tagging stays off (git.tag_format cleared)",
        )


def _translate_branch(work: _Translation) -> None:
    """
    The one v7 default worth *not* carrying over.

    Every other default describes what the project was released under, so
    reproducing it keeps behaviour. `branch = master` describes what v7 guessed
    when nobody said otherwise -- and on a repo that renamed its default branch
    it is simply wrong, pinning a branch that does not exist and failing the
    first bump against `on_wrong_branch = error`. A branch the project wrote
    down is a decision and gets pinned; an unwritten one is left to vommit's
    own auto-detect, which resolves the real default branch when the config is
    written.
    """
    if work.is_explicit("branch"):
        work.assign("branch", "git.branch", str(work.get("branch")))
        return

    work.note_lossy(
        "branch",
        f"unset, so v7 released from {str(work.get('branch'))!r}; leaving "
        f"git.branch at {DERIVE_BRANCH} to detect the real default branch",
    )


def _translate_changelog(work: _Translation) -> None:
    work.assign("changelog_file", "changelog.file", str(work.get("changelog_file")))
    work.assign(
        "changelog_placeholder",
        "changelog.placeholder",
        str(work.get("changelog_placeholder")),
    )
    _translate_sections(work)


def _translate_sections(work: _Translation) -> None:
    levels: dict[str, str] = {}
    dropped: list[str] = []

    for section in split_fields(work.get("changelog_sections")):
        change_type = SECTION_TYPES.get(section.lower(), section.lower())
        if change_type == CATCH_ALL_SECTION or not RE_CHANGE_TYPE.match(change_type):
            dropped.append(section)
            continue
        levels[change_type] = LEVEL_TITLES.get(
            change_type, f"{change_type.capitalize()}{{s}}"
        )

    if not levels:
        work.note_lossy(
            "changelog_sections",
            "no section translated to a commit type; keeping vommit's defaults",
        )
        return

    work.changelog.levels = levels
    work.mapped.append(
        Note(
            key="changelog_sections",
            detail=f"changelog.levels = {', '.join(levels)}",
        )
    )

    # only worth saying when the project chose these sections itself
    if dropped and work.is_explicit("changelog_sections"):
        work.note_lossy(
            "changelog_sections",
            f"dropped, no commit type behind them: {', '.join(dropped)}",
        )


def _translate_commands(work: _Translation) -> None:
    if work.is_explicit("build_command"):
        work.assign("build_command", "commands.build", str(work.get("build_command")))

    dist_path = str(work.get("dist_path")).rstrip("/")
    work.assign("dist_path", "commands.clean", f"rm -rf ./{dist_path}")

    if not work.get("remove_dist"):
        # an empty command is how a step is switched off
        work.assign(
            "remove_dist",
            "commands.clean",
            "",
            detail="release no longer cleans dist first",
        )


def _translate_pypi(work: _Translation) -> None:
    """
    v7 had two switches for one thing; either one being off means no upload.
    """
    keys = [
        key
        for key in ("upload_to_pypi", "upload_to_repository")
        if not work.get(key) and work.is_explicit(key)
    ]
    if keys:
        work.assign(
            " / ".join(keys),
            "pypi.enabled",
            False,
            detail="publishing stays off (pypi.enabled = false)",
        )


def _translate_bumps(work: _Translation) -> None:
    """
    v7 resolved a commit in this order (`history/parser_angular.py:80`):
    breaking beats `minor_types` beats `patch_types` beats the default bump,
    and a type outside `allowed_types` never parsed at all.
    """
    default_bump = str(work.get("parser_angular_default_level_bump")).lower()
    if default_bump not in LEVEL_BUMPS:
        raise VommitError(
            f"parser_angular_default_level_bump is {default_bump}; "
            f"expected one of {', '.join(LEVEL_BUMPS)}."
        )

    minor = split_fields(work.get("parser_angular_minor_types"))
    patch = split_fields(work.get("parser_angular_patch_types"))
    fallback = LEVEL_BUMPS[default_bump]

    bumps: dict[str, VersionBump] = {BREAKING: "major"}
    for change_type in split_fields(work.get("parser_angular_allowed_types")):
        if change_type in minor:
            bumps[change_type] = "minor"
        elif change_type in patch:
            bumps[change_type] = "patch"
        elif fallback:
            bumps[change_type] = fallback

    work.config.version_bump_map = bumps
    work.mapped.append(
        Note(
            key="parser_angular_*",
            detail="version_bump_map = "
            + ", ".join(f"{name}:{level}" for name, level in bumps.items()),
        )
    )

    if dropped := [name for name in Config.version_bump_map if name not in bumps]:
        work.note_lossy(
            "parser_angular_*",
            f"vommit releases on {', '.join(dropped)} by default, v7 did not; "
            f"use `vommit bump --patch` when you want one anyway",
        )


def _translate_prerelease(work: _Translation) -> None:
    tag = str(work.get("prerelease_tag")).lower()
    token = PRERELEASE_TOKENS.get(tag)
    if token is None:
        work.note_lossy(
            "prerelease_tag",
            f"{tag!r} is not a PEP 440 prerelease segment; "
            f"keeping {work.config.prerelease_token!r}",
        )
        return
    work.assign("prerelease_tag", "prerelease_token", token)


def _translate_version_files(work: _Translation) -> None:
    for entry in split_fields(work.get("version_variable")):
        if version_file := VersionFile.parse(entry):
            work.version_files.append(version_file)
            continue
        work.note_lossy(
            "version_variable", f"{entry!r} is not `path:variable`; skipped"
        )


def _translate_lossy(work: _Translation) -> None:
    """
    Explicit settings that came across differently, or not at all.
    """
    if work.is_explicit("commit_message") and str(work.get("commit_message")).strip():
        work.note_lossy(
            "commit_message",
            "release commits carry a subject only; the body is dropped",
        )

    if work.is_explicit("major_on_zero") and not work.get("major_on_zero"):
        work.note_lossy(
            "major_on_zero",
            "vommit always bumps major on a breaking change, including below 1.0.0",
        )

    if work.is_explicit("patch_without_tag") and work.get("patch_without_tag"):
        work.note_lossy(
            "patch_without_tag",
            "commits vommit cannot parse never trigger a release",
        )

    version_source = work.get("version_source")
    if work.is_explicit("version_source") and version_source != "commit":
        work.note_lossy(
            "version_source",
            f"{version_source!r} ignored; vommit reads [project].version "
            f"and uses tags only to find the previous release",
        )

    if work.is_explicit("version_toml"):
        work.mapped.append(
            Note(
                key="version_toml",
                detail="already points at [project].version, where vommit expects it",
            )
        )


def _collect_unsupported(work: _Translation) -> None:
    """
    Everything the project wrote down that translation never looked at.

    v7 ignored unknown keys without a word, so real configs carry typos that
    never did anything -- hence the did-you-mean rather than a bare "unknown".
    """
    for key in sorted(work.raw.explicit - HANDLED_KEYS):
        if reason := UNSUPPORTED_KEYS.get(key):
            work.note_unsupported(key, reason)
            continue
        work.note_unsupported(key, _unknown_detail(key))


def _unknown_detail(key: str) -> str:
    detail = "not a python-semantic-release setting; v7 ignored it silently"
    if suggestions := difflib.get_close_matches(key, sorted(KNOWN_KEYS), n=1):
        return f"{detail}. Did you mean {suggestions[0]}?"
    return detail


def plan_static_version(
    pyproject: Path,
    version_files: t.Iterable[VersionFile],
    fallback: str | None = None,
) -> VersionTransform | None:
    """
    The `[project]` edits that make a dynamic-version project bumpable, if any.

    `uv version` -- which every bump goes through -- refuses a project that
    declares `dynamic = ["version"]`, whatever the backend. v7 projects are
    usually shaped that way: `version_variable` exists precisely because the
    backend reads the version out of a source file instead of `[project]`.

    None when there is nothing to do; `VommitError` when the project is dynamic
    in a way this cannot undo, because writing a config that can never bump is
    worse than refusing.
    """
    document = read_toml(pyproject)
    project = document.get("project")
    if not isinstance(project, dict):
        # no PEP 621 metadata at all: there is nowhere to put a version, and
        # inventing a [project] table claims more than a migration should.
        return None

    dynamic = [str(entry) for entry in project.get("dynamic", [])]
    if "version" not in dynamic:
        if "version" in project:
            return None
        # v7 predates PEP 621 and was happy with the version living only in the
        # source file. `uv version` is not: without [project].version there is
        # nothing to read, let alone bump.
        version, origin = _discover_version(pyproject.parent, version_files, fallback)
        return VersionTransform(version=version, source=origin)

    backend = _detect_backend(document)
    hook_key = HOOK_KEYS.get(backend)
    if hook_key is None:
        raise VommitError(
            f"this project builds with {backend or 'an unrecognised backend'} and "
            f"declares a dynamic version, which vommit cannot bump. Give "
            f"[project] a static version first, then migrate."
        )

    if backend == "hatchling" and _table_at(document, "tool.hatch.version"):
        source = str(_table_at(document, "tool.hatch.version").get("source", ""))
        if source:
            raise VommitError(
                f"[tool.hatch.version] takes its version from {source!r}; there is "
                f"no literal to turn into a static [project].version."
            )

    version, origin = _discover_version(pyproject.parent, version_files, fallback)
    return VersionTransform(
        version=version, source=origin, backend=backend, hook_key=hook_key
    )


def plan_version_migration(
    pyproject: Path,
    version_files: t.Iterable[VersionFile],
    fallback: str | None = None,
) -> VersionPlan:
    """
    Everything the migration does to the version, decided in one place.

    Refuses the one combination that destroys information: rewriting the source
    literal away when `[project]` cannot hold the version it replaced. That is
    not a hypothetical -- a v7 project with no `[project].version` and no
    `dynamic` keeps its only version in the file about to be rewritten.
    """
    rewrites = tuple(version_files)
    transform = plan_static_version(pyproject, rewrites, fallback)

    if rewrites and transform is None and not _has_static_version(pyproject):
        raise VommitError(
            f"rewriting {', '.join(entry.path for entry in rewrites)} would "
            f"leave this project with no version at all: there is no [project] "
            f"table to freeze one into. Add [project] to pyproject.toml first, "
            f"then migrate."
        )

    return VersionPlan(
        transform=transform,
        rewrites=rewrites,
        warnings=tuple(_rewrite_warnings(rewrites, project_name(pyproject))),
    )


def _has_static_version(pyproject: Path) -> bool:
    project = read_toml(pyproject).get("project")
    return isinstance(project, dict) and "version" in project


def _rewrite_warnings(
    version_files: t.Iterable[VersionFile],
    distribution: str | None,
) -> list[Note]:
    """
    Where `version(__package__)` will not resolve to the distribution.

    `importlib.metadata.version` looks up a *distribution*, while `__package__`
    names the *import* package. They usually normalise to the same string,
    which is why the idiom works at all -- but when they do not, the rewritten
    file raises `PackageNotFoundError` on import, long after this ran.
    """
    if not distribution:
        return []

    notes: list[Note] = []
    wanted = normalise_name(distribution)
    for version_file in version_files:
        package = _import_package(version_file)
        literal = f'{version_file.variable} = version("{distribution}")'
        if package is None:
            notes.append(
                Note(
                    version_file.path,
                    f"a top-level module has no `__package__` to look up; "
                    f"write `{literal}` instead",
                )
            )
        elif normalise_name(package) != wanted:
            notes.append(
                Note(
                    version_file.path,
                    f"`version(__package__)` looks up {package!r}, but the "
                    f"distribution is {distribution!r}; write `{literal}` "
                    f"instead if the import fails",
                )
            )
    return notes


def _import_package(version_file: VersionFile) -> str | None:
    """
    What `__package__` holds inside this file, or None for a top-level module.

    A `src` directory is a layout convention rather than a package, so it never
    counts towards the dotted name -- a project that really ships a package
    called `src` gets one spurious warning.
    """
    parts = PurePosixPath(version_file.path).parts[:-1]
    package = [part for part in parts if part not in {".", "src"}]
    return ".".join(package) or None


def _detect_backend(document: t.Any) -> str:
    """
    The build backend, by declaration first and by tool table second.
    """
    build_system = document.get("build-system")
    declared = (
        str(build_system.get("build-backend", ""))
        if isinstance(build_system, dict)
        else ""
    )
    if backend := BUILD_BACKENDS.get(declared):
        return backend

    for backend, hook_key in HOOK_KEYS.items():
        if _table_at(document, hook_key.rsplit(".", 1)[0]) is not None:
            return backend
    return declared


def _discover_version(
    root: Path,
    version_files: t.Iterable[VersionFile],
    fallback: str | None,
) -> tuple[str, str]:
    """
    The version to freeze into `[project]`, and where it was found.

    The source file wins: that literal is what v7 has been bumping, so it is
    the project's own answer. A tag is the fallback for a project whose file
    has already drifted or gone.
    """
    for version_file in version_files:
        found = version_file.read(root)
        if parse_version(found):
            return t.cast(str, found), version_file.path

    if parse_version(fallback):
        return t.cast(str, fallback), "the latest tag"

    raise VommitError(
        "no version to freeze: neither the version_variable file nor a tag "
        "yielded a PEP 440 version. Set [project].version by hand, then migrate."
    )


def apply_static_version(pyproject: Path, transform: VersionTransform) -> None:
    """
    Carry out `transform`: static version in, `dynamic` and the hook table out.
    """
    document = read_toml(pyproject)
    project = document["project"]

    project["version"] = transform.version

    if transform.hook_key:
        remaining = [
            entry for entry in project.get("dynamic", []) if str(entry) != "version"
        ]
        if remaining:
            project["dynamic"] = remaining
        else:
            del project["dynamic"]
        _remove_nested(document, transform.hook_key)

    pyproject.write_text(tomlkit.dumps(document))


def rewrite_version_files(
    version_files: t.Iterable[VersionFile],
    root: Path,
) -> RewriteResult:
    """
    Point the files v7 rewrote at the installed metadata instead.

    `__version__ = "1.2.3"` becomes `__version__ = version(__package__)`, so the
    literal stops being a second source of truth that nothing updates any more.
    """
    changed: list[Path] = []
    skipped: list[Note] = []

    for version_file in version_files:
        target = root / version_file.path
        if not target.exists():
            skipped.append(Note(version_file.path, "no such file"))
            continue
        if target.suffix != ".py":
            skipped.append(
                Note(version_file.path, "not a Python file; rewrite it yourself")
            )
            continue

        original = target.read_text()
        rewritten = _rewrite_assignment(original, version_file)
        if rewritten is None:
            skipped.append(
                Note(version_file.path, f"no {version_file.variable} assignment found")
            )
            continue

        target.write_text(rewritten)
        changed.append(target)

    return RewriteResult(changed=changed, skipped=skipped)


def _rewrite_assignment(content: str, version_file: VersionFile) -> str | None:
    """
    None when the file holds no assignment v7 would have recognised.
    """
    match = version_file.pattern.search(content)
    if not match:
        return None

    prefix = match.group("prefix")
    indent = prefix[: len(prefix) - len(prefix.lstrip())]
    # directly above the assignment rather than at the top of the file: legal
    # wherever the assignment is, and it never has to reason about a docstring.
    header = "" if METADATA_IMPORT in content else f"{indent}{METADATA_IMPORT}\n\n"
    replacement = f"{header}{indent}{version_file.variable} = version(__package__)"
    return content[: match.start()] + replacement + content[match.end() :]


def strip_psr(pyproject: Path) -> list[str]:
    """
    Remove the v7 config and dependency; returns what went, for the report.

    Two live release configs in one file is the failure mode worth avoiding:
    whichever tool runs next looks authoritative and neither is.
    """
    document = read_toml(pyproject)
    removed: list[str] = []

    if _remove_nested(document, PSR_KEY):
        removed.append(f"[{PSR_KEY}]")

    for label, table, key in _dependency_lists(document):
        # removed in place rather than replaced wholesale: assigning a fresh
        # list would reflow a carefully broken-up array onto one line.
        requirements = table[key]
        touched = False
        for requirement in list(requirements):
            if _requirement_name(str(requirement)) != PSR_DISTRIBUTION:
                continue
            requirements.remove(requirement)
            removed.append(f"{PSR_DISTRIBUTION} from {label}")
            touched = True

        # an array emptied this way keeps its line break, leaving `[\n]` behind;
        # one that was already empty is left exactly as the project wrote it.
        if touched and not requirements:
            table[key] = tomlkit.array()

    if removed:
        pyproject.write_text(tomlkit.dumps(document))
    return removed


def _dependency_lists(document: t.Any) -> t.Iterator[tuple[str, t.Any, str]]:
    """
    Every list of requirements a project can spell, as (label, table, key).
    """
    project = document.get("project")
    if isinstance(project, dict) and isinstance(project.get("dependencies"), list):
        yield "project.dependencies", project, "dependencies"

    for parent_key, prefix in (
        ("optional-dependencies", "project.optional-dependencies"),
        ("dependency-groups", "dependency-groups"),
    ):
        parent = (
            project.get(parent_key)
            if prefix.startswith("project.") and isinstance(project, dict)
            else document.get(parent_key)
        )
        if not isinstance(parent, dict):
            continue
        for group, requirements in parent.items():
            if isinstance(requirements, list):
                yield f"{prefix}.{group}", parent, str(group)


def normalise_name(name: str) -> str:
    """
    A distribution name in the one spelling PEP 503 compares.
    """
    return RE_NAME_SEPARATORS.sub("-", name).lower()


def _requirement_name(requirement: str) -> str:
    """
    The distribution a requirement string names, normalised per PEP 503.
    """
    match = RE_REQUIREMENT_NAME.match(requirement.strip())
    return normalise_name(match.group()) if match else ""


def _table_at(document: t.Any, dotted_key: str) -> t.Any:
    node = document
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _remove_nested(document: t.Any, dotted_key: str) -> bool:
    """
    Delete a (dotted) table, and any parent it leaves empty.
    """
    parts = dotted_key.split(".")
    tables: list[t.Any] = [document]
    for part in parts[:-1]:
        node = tables[-1].get(part)
        if not isinstance(node, dict):
            return False
        tables.append(node)

    if parts[-1] not in tables[-1]:
        return False
    del tables[-1][parts[-1]]

    for parent, key in zip(reversed(tables[:-1]), reversed(parts[:-1])):
        if parent[key]:
            break
        del parent[key]
    return True
