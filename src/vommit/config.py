import datetime as dt
import re
import string
import typing as t
from pathlib import Path

import tomlkit
from configuraptor import Defaultable, TypedConfig

from .commits import (
    BREAKING,
    CommitEntry,
    CommitHeader,
    VersionBump,
    has_breaking_footer,
    parse_commit,
)
from .helpers import read_toml
from .interactive import InteractiveConfig
from .versioning import DEFAULT_PRERELEASE_TOKEN, PrereleaseToken

TOML_KEY = "tool.vommit"

# special branch values, resolved by `GitRepo.resolve_branch`
DERIVE_BRANCH: t.Final = "<head>"  # look up the main branch (remote, then local)
CURRENT_BRANCH: t.Final = "<current>"  # release from whichever branch we are on

RE_WHITESPACE_RUN = re.compile(r"\s+")


def today(tz: dt.tzinfo | None = None) -> dt.date:
    return dt.datetime.now(tz=tz).date()


def _to_plain_mapping(value: t.Any) -> t.Any:
    if isinstance(value, dict):
        return {k: _to_plain_mapping(v) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        return [_to_plain_mapping(v) for v in value]
    elif hasattr(value, "__dict__"):
        if getattr(value, "enabled", None) is False:
            return {"enabled": False}

        return {
            k: _to_plain_mapping(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    else:
        return value


def _ensure_toml_table(parent: t.Any, key: str) -> t.Any:
    if key not in parent or not isinstance(parent[key], dict):
        parent[key] = tomlkit.table()
    return parent[key]


def _merge_toml_table_in_place(toml_table: t.Any, data: dict[str, t.Any]) -> None:
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, dict):
            child = _ensure_toml_table(toml_table, key)
            _merge_toml_table_in_place(child, value)
            continue
        toml_table[key] = tomlkit.item(value)


def _get_nested_mapping(node: t.Any, dotted_key: str) -> dict[str, t.Any] | None:
    current = node
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, dict) else None


def _collect_leaf_key_paths(data: dict[str, t.Any], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for key, value in data.items():
        current = f"{prefix}.{key}".strip(".")
        if isinstance(value, dict):
            paths |= _collect_leaf_key_paths(value, current)
            continue
        paths.add(current)
    return paths


class GitConfig(TypedConfig, Defaultable):
    enabled: t.Annotated[bool, "Enable git integration?"] = True
    origin: t.Annotated[str, "Git remote name"] = "origin"

    # '<head>' looks the main branch up (first remote, then local config),
    # '<current>' releases from whichever branch is checked out.
    branch: t.Annotated[
        str | t.Literal["<head>", "<current>"],
        "Release branch (<head> to auto-detect; <current> to stay on the branch)",
    ] = DERIVE_BRANCH
    on_wrong_branch: t.Annotated[
        t.Literal["error", "warn", "switch"],
        "If on wrong branch",
    ] = "error"

    # set to None to disable tagging/committing:
    tag_format: t.Annotated[str | None, "Git tag format (empty to disable)"] = (
        "v{version}"
    )
    commit_format: t.Annotated[
        str | None, "Git commit message format (empty to disable)"
    ] = "{version}"

    def format_tag(self, version: str) -> str | None:
        """
        None when tagging is switched off (empty or missing `tag_format`).
        """
        return self.tag_format.format(version=version) if self.tag_format else None

    def format_commit(self, version: str) -> str | None:
        """
        None when committing is switched off (empty or missing `commit_format`).
        """
        return (
            self.commit_format.format(version=version) if self.commit_format else None
        )

    @property
    def tag_glob(self) -> str | None:
        """
        Shell glob matching any tag produced by `tag_format`, for `git describe --match`.
        """
        return self.tag_format.replace("{version}", "*") if self.tag_format else None

    @property
    def tag_re(self) -> re.Pattern[str] | None:
        """
        `tag_format` inverted, so a tag can be read back as a version.
        """
        if not self.tag_format:
            return None
        literals = self.tag_format.split("{version}")
        pattern = "(.+)".join(re.escape(part) for part in literals)
        return re.compile(rf"^{pattern}$")

    def parse_tag(self, tag: str) -> str | None:
        """
        The version inside `tag`, or None if it was not made by `tag_format`.

        Without a `tag_format` the tag is taken to be the version itself, which
        is the best guess available for repos that tag by hand.
        """
        matcher = self.tag_re
        if matcher is None:
            return tag
        match = matcher.match(tag)
        return match.group(1) if match else None


class ChangelogConfig(TypedConfig, Defaultable):
    enabled: t.Annotated[bool, "Enable changelog updates?"] = True
    file: t.Annotated[str, "Changelog file path"] = "CHANGELOG.md"

    # off by default: an entry per rc is noise, and the same changes end up
    # listed twice once the release lands. Kept aggregated until then instead.
    include_prereleases: t.Annotated[
        bool, "Give prereleases their own changelog entry?"
    ] = False

    levels: dict[str, str] = {
        # note: use {} syntax for pluralization
        # 'break' is not a commit type; it collects commits marked as breaking.
        "break": "Breaking Change{s}",
        "feat": "Feature{s}",
        "fix": "Fix{es}",
        "perf": "Performance",
        "docs": "Documentation",
    }

    placeholder: t.Annotated[str, "Placeholder marker in CHANGELOG"] = (
        "<!-- next-version-placeholder -->"
    )
    placeholder_regex: t.Annotated[
        str,
        "Optional placeholder regex override in CHANGELOG",
        {
            "derive_default": {
                "from": "placeholder",
                "with": "build_placeholder_regex",
                "when": "<auto>",
            }
        },
    ] = "<auto>"
    entry_title_format: t.Annotated[str, "Changelog entry title format"] = (
        "## v{version} ({date:%Y-%m-%d})"
    )

    @property
    def placeholder_re(self) -> re.Pattern[str]:
        """
        Derived lazily: `__post_init__` does not run for configs nested in a
        parent `Config.load`, so anything derived at init time is unreliable.
        """
        cached = getattr(self, "_placeholder_re", None)
        if cached is None:
            cached = re.compile(self.effective_placeholder_regex, flags=re.MULTILINE)
            self._placeholder_re = cached
        return cached

    @property
    def effective_placeholder_regex(self) -> str:
        if self.placeholder_regex in {"", "<auto>"}:
            return self.build_placeholder_regex(self.placeholder)
        return self.placeholder_regex

    @staticmethod
    def build_placeholder_regex(placeholder: str) -> str:
        # escape each chunk separately so re.escape never touches the
        # whitespace itself (it would keep the literal space, doubling
        # the backslash once the pattern gets substituted in below).
        # horizontal whitespace only: a marker lives on one line.
        chunks = RE_WHITESPACE_RUN.split(placeholder.strip())
        flexible_whitespace = r"[ \t]+".join(re.escape(chunk) for chunk in chunks)
        return rf"^{flexible_whitespace}$"

    def format_entry_title(self, version: str, date: dt.date | None = None) -> str:
        return self.entry_title_format.format(version=version, date=date or today())

    def entry_title_re(self, version: str | None = None) -> re.Pattern[str]:
        """
        `entry_title_format` inverted, to find an entry that was written earlier.

        The date is a wildcard: an entry carries the date it was released on,
        which is rarely the date anyone comes looking for it. Pass a `version` to
        match one entry, or leave it out to match the start of any entry.
        """
        pattern = ""
        for literal, field, _, _ in string.Formatter().parse(self.entry_title_format):
            pattern += re.escape(literal)
            if field == "version" and version is not None:
                pattern += re.escape(version)
            elif field is not None:
                pattern += r".+?"
        return re.compile(rf"^{pattern}$", flags=re.MULTILINE)

    def resolve_path(self, root: str | Path) -> Path:
        changelog_file = Path(self.file)
        if changelog_file.is_absolute():
            return changelog_file
        return Path(root) / changelog_file

    def pluralize(self, level: str, count: int) -> str:
        template = self.levels[level]

        # Placeholder strategy:
        # - singular (count == 1): remove suffix placeholders
        # - plural (count != 1): use placeholder name as suffix (e.g. {s}->{s}, {es}->{es})
        formatter = string.Formatter()
        field_names = {
            field_name
            for _, field_name, _, _ in formatter.parse(template)
            if field_name
        }
        forms = {name: (name if count != 1 else "") for name in field_names}
        return template.format(**forms)


class PypiConfig(TypedConfig, Defaultable):
    enabled: t.Annotated[bool, "Enable PyPI publishing?"] = True
    # off means "whatever the environment already provides": vommit then sets no
    # credentials of its own and an ambient UV_PUBLISH_TOKEN still works.
    use_keyring: t.Annotated[bool, "Use keyring for PyPI auth?"] = True


class CommandConfig(TypedConfig, Defaultable):
    """
    The three commands a release runs, in that order.

    Leave one empty to skip it: a project that has nothing to clean sets
    `clean = ""` rather than finding a command that does nothing.
    """

    clean: t.Annotated[str, "Clean command (empty to skip)"] = "rm -rf ./dist"
    build: t.Annotated[str, "Build command (empty to skip)"] = "uv build"
    publish: t.Annotated[str, "Publish command (empty to skip)"] = "uv publish"
    # runs only when `publish` did, so it can assume there is something to tidy
    post_publish: t.Annotated[str, "Command to run after publishing"] = ""


def _if_enabled[SectionT: TypedConfig](section: SectionT | None) -> SectionT | None:
    """
    A section is off when it is absent *or* explicitly disabled; callers should
    not have to know that both spellings exist.
    """
    return (
        section if section is not None and getattr(section, "enabled", True) else None
    )


class Config(InteractiveConfig, Defaultable):
    git: t.Annotated[GitConfig | None, "Git settings"]
    changelog: t.Annotated[ChangelogConfig | None, "Changelog settings"]
    pypi: t.Annotated[PypiConfig | None, "PyPI settings"]
    commands: t.Annotated[CommandConfig | None, "Command settings"]

    # feat!(scope): ... to bump major
    allow_breaking_bang: t.Annotated[
        bool, "Treat ! in commit header as major bump?"
    ] = True
    # BREAKING CHANGE: ... to bump major
    allow_breaking_footer: t.Annotated[
        bool, "Treat BREAKING CHANGE footer as major bump?"
    ] = True

    # PEP 440 spells these a1/b1/rc1; `uv version --bump` takes the long names.
    prerelease_token: t.Annotated[
        PrereleaseToken, "Prerelease segment used by `bump --prerelease`"
    ] = DEFAULT_PRERELEASE_TOKEN

    confirm: t.Annotated[bool, "Ask before writing a release?"] = True

    version_bump_map: dict[str, VersionBump] = {
        BREAKING: "major",
        "feat": "minor",
        "fix": "patch",
        "perf": "patch",
        "docs": "patch",
    }

    @property
    def active_git(self) -> GitConfig | None:
        return _if_enabled(self.git)

    @property
    def active_changelog(self) -> ChangelogConfig | None:
        return _if_enabled(self.changelog)

    @property
    def active_pypi(self) -> PypiConfig | None:
        return _if_enabled(self.pypi)

    def tag_version(self, tag: str | None) -> str | None:
        """
        The version a tag stands for, per the configured `tag_format`.

        Reads `git` rather than `active_git`: switching git integration off stops
        vommit from tagging, but says nothing about how existing tags are named.
        """
        if not tag:
            return None
        return self.git.parse_tag(tag) if self.git else tag

    def is_breaking(self, message: str, header: CommitHeader | None) -> bool:
        if self.allow_breaking_footer and has_breaking_footer(message):
            return True
        return bool(self.allow_breaking_bang and header and header.bang)

    def resolve_version_bump(self, change_level: str) -> VersionBump | None:
        return self.version_bump_map.get(change_level)

    def resolve_version_bump_from_commit(
        self,
        commit_subject: str,
    ) -> VersionBump | None:
        header = parse_commit(commit_subject)
        if self.is_breaking(commit_subject, header):
            return "major"
        if not header:
            return None

        return self.resolve_version_bump(header.type)

    def commit_entries(self, messages: t.Iterable[str]) -> list[CommitEntry]:
        """
        Conventional commits, in log order, annotated with breaking-change policy.
        """
        entries = []
        for message in messages:
            header = parse_commit(message)
            if not header or not header.description:
                continue
            entries.append(
                CommitEntry(
                    type=header.type,
                    scope=header.scope,
                    description=header.description,
                    breaking=self.is_breaking(message, header),
                )
            )
        return entries

    @classmethod
    def from_pyproject_path(
        cls,
        pyproject: Path,
        toml_key: str = TOML_KEY,
    ) -> t.Self:
        if not pyproject.exists():
            return cls.default()
        return cls.load(pyproject.absolute(), key=toml_key, convert_types=True)

    @classmethod
    def from_pyproject(
        cls,
        root: str | Path | None = None,
        toml_key: str = TOML_KEY,
    ) -> t.Self:
        base = Path(root) if root is not None else Path.cwd()
        return cls.from_pyproject_path(base / "pyproject.toml", toml_key=toml_key)

    @classmethod
    def has_pyproject_config(
        cls,
        pyproject: str | Path | None = None,
        toml_key: str = TOML_KEY,
    ) -> bool:
        pyproject_path = (
            Path(pyproject) if pyproject is not None else Path.cwd() / "pyproject.toml"
        )
        if not pyproject_path.exists():
            return False
        doc = read_toml(pyproject_path)
        return _get_nested_mapping(doc, toml_key) is not None

    @classmethod
    def configured_key_paths(
        cls,
        pyproject: str | Path | None = None,
        toml_key: str = TOML_KEY,
    ) -> set[str]:
        pyproject_path = (
            Path(pyproject) if pyproject is not None else Path.cwd() / "pyproject.toml"
        )
        if not pyproject_path.exists():
            return set()
        doc = read_toml(pyproject_path)
        table = _get_nested_mapping(doc, toml_key)
        if table is None:
            return set()
        return _collect_leaf_key_paths(table)

    @classmethod
    def is_complete(
        cls,
        pyproject: str | Path | None = None,
        toml_key: str = TOML_KEY,
    ) -> bool:
        if not cls.has_pyproject_config(pyproject=pyproject, toml_key=toml_key):
            return False
        configured = cls.configured_key_paths(pyproject=pyproject, toml_key=toml_key)
        required = cls.interactive_key_paths()
        return required.issubset(configured)

    def write_to_pyproject(
        self,
        pyproject: str | Path | None = None,
        toml_key: str = TOML_KEY,
    ) -> None:
        pyproject_path = (
            Path(pyproject) if pyproject is not None else Path.cwd() / "pyproject.toml"
        )
        doc = (
            read_toml(pyproject_path) if pyproject_path.exists() else tomlkit.document()
        )

        key_parts = toml_key.split(".")
        target = doc
        for part in key_parts[:-1]:
            target = _ensure_toml_table(target, part)

        # write toml in a way that we keep comments, whitespace intact:

        root = _ensure_toml_table(target, key_parts[-1])
        payload = _to_plain_mapping(self)
        _merge_toml_table_in_place(root, payload)

        pyproject_path.write_text(tomlkit.dumps(doc) + "\n")
