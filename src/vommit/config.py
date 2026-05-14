import re
import shlex
import string
import subprocess
import typing as t
from datetime import date
from pathlib import Path

import tomlkit
from configuraptor import Defaultable, TypedConfig

from .interactive import InteractiveConfig

TOML_KEY = "tool.vommit"
DERIVE_BRANCH = "<head>"
VersionBump = t.Literal["major", "minor", "patch"]

RE_HEAD = re.compile(r"refs/heads/(\S+)")
RE_COMMIT_HEADER = re.compile(
    r"^(?P<type>[A-Za-z][\w-]*)(?P<bang_before_scope>!)?"
    r"(?:\((?P<scope>[^)]+)\))?(?P<bang_after_scope>!)?:",
)
RE_BREAKING_CHANGE_FOOTER = re.compile(r"(?im)^(?:BREAKING[ -]CHANGE):\s+.+$")
RE_WHITESPACE_RUN = re.compile(r"\s+")


def bash(command: str) -> tuple[int, str, str]:
    pipes = subprocess.Popen(
        shlex.split(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout = pipes.stdout.read().decode() if pipes.stdout else ""
    stderr = pipes.stderr.read().decode() if pipes.stderr else ""

    return pipes.returncode, stdout, stderr


def throw(error: Exception) -> t.Never:
    """
    Functional raise, useful for if ... else ... or callbacks.
    """
    raise error


def _to_plain_mapping(value: t.Any) -> t.Any:
    if isinstance(value, dict):
        return {k: _to_plain_mapping(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain_mapping(v) for v in value]
    if hasattr(value, "__dict__"):
        if getattr(value, "enabled", None) is False:
            return {"enabled": False}
        return {
            k: _to_plain_mapping(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
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


def find_main_branch_upstream(origin: str = "origin") -> str | None:
    origin = shlex.quote(origin)
    _, stdout, _ = bash(f"git ls-remote --symref {origin} HEAD")

    match = RE_HEAD.search(stdout)
    return match.group(1) if match else None


def find_main_branch_local():
    _, stdout, _ = bash("git config --get init.defaultBranch")
    return stdout.strip()


def find_main_branch(
    origin: str,
    remote: bool = True,
    local: bool = True,
) -> str | None:
    return (
        (remote and find_main_branch_upstream(origin))
        or (local and find_main_branch_local())
        or None
    )


class GitConfig(TypedConfig, Defaultable):
    enabled: t.Annotated[bool, "Enable git integration?"] = True
    origin: t.Annotated[str, "Git remote name"] = "origin"

    # special '<head>' does lookup (first remote, then local config)
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

    def __post_init__(self):
        if self.branch == DERIVE_BRANCH:
            self.branch = find_main_branch(
                self.origin,
                remote=True,
                local=True,
            ) or throw(
                ValueError(
                    "main git branch could not be derived; Please set it via `tool.vommit.git.branch`",
                ),
            )

    def format_tag(self, version: str) -> str | None:
        return self.tag_format and self.tag_format.format(version=version)

    def format_commit(self, version: str) -> str | None:
        return self.commit_format and self.commit_format.format(version=version)


class ChangelogConfig(TypedConfig, Defaultable):
    enabled: t.Annotated[bool, "Enable changelog updates?"] = True
    file: t.Annotated[str, "Changelog file path"] = "CHANGELOG.md"

    levels: dict[str, str] = {
        # note: use {} syntax for pluralization
        "feat": "Feature{s}",
        "fix": "Bug Fix{es}",
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

    def __post_init__(self):
        effective_regex = (
            self.build_placeholder_regex(self.placeholder)
            if self.placeholder_regex in {"", "<auto>"}
            else self.placeholder_regex
        )
        self._placeholder_re = re.compile(effective_regex, flags=re.MULTILINE)

    @staticmethod
    def build_placeholder_regex(placeholder: str) -> str:
        escaped = re.escape(placeholder.strip())
        flexible_whitespace = RE_WHITESPACE_RUN.sub(r"\\s*", escaped)
        return rf"^{flexible_whitespace}$"

    def apply_placeholder(self, changelog: str, to_insert: str) -> str | None:
        # Keep marker and insert content right below it.
        match = self._placeholder_re.search(changelog)
        if not match:
            return None

        marker = match.group(0)
        insertion = to_insert.strip("\n")
        replacement = f"{marker}\n\n{insertion}"

        return self._placeholder_re.sub(replacement, changelog, count=1)

    def format_entry_title(self, version: str, today: date | None = None) -> str:
        return self.entry_title_format.format(
            version=version, date=today or date.today()
        )

    def ensure_file(self, base_dir: str | Path | None = None) -> Path | None:
        if not self.enabled:
            return None

        base = Path(base_dir) if base_dir is not None else Path.cwd()
        changelog_file = Path(self.file)
        if not changelog_file.is_absolute():
            changelog_file = base / changelog_file

        if not changelog_file.exists():
            changelog_file.parent.mkdir(parents=True, exist_ok=True)
            with changelog_file.open("w") as f:
                f.write("# Changelog\n\n")
                f.write(f"{self.placeholder}\n")

        return changelog_file

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
    username: t.Annotated[str, "PyPI username"] = "__token__"
    use_keyring: t.Annotated[bool, "Use keyring for PyPI auth?"] = True


class CommandConfig(TypedConfig, Defaultable):
    clean: t.Annotated[str, "Clean command"] = "rm -r ./dist"
    build: t.Annotated[str, "Build command"] = "uv build"
    publish: t.Annotated[str, "Publish command"] = "uv publish"

    release: t.Annotated[
        str,
        "Release pipeline command",
        {"interactive": False},
    ] = "{clean} && {build} && {publish}"

    @property
    def release_command(self) -> str:
        return self.release.format(
            clean=self.clean,
            build=self.build,
            publish=self.publish,
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

    # todo:
    #  - compatibility/migration from old `tool.semantic_release` config

    version_bump_map: dict[str, VersionBump] = {
        "break": "major",
        "feat": "minor",
        "fix": "patch",
        "perf": "patch",
        "docs": "patch",
    }

    def resolve_version_bump(self, change_level: str) -> VersionBump | None:
        return self.version_bump_map.get(change_level)

    def resolve_version_bump_from_commit(
        self,
        commit_subject: str,
    ) -> VersionBump | None:
        commit_text = commit_subject.strip()
        if self.allow_breaking_footer and RE_BREAKING_CHANGE_FOOTER.search(commit_text):
            return "major"

        header = commit_text.splitlines()[0] if commit_text.splitlines() else ""
        match = RE_COMMIT_HEADER.match(header)
        if not match:
            return None

        if self.allow_breaking_bang and (
            match.group("bang_before_scope") or match.group("bang_after_scope")
        ):
            return "major"

        change_level = match.group("type").lower()
        return self.resolve_version_bump(change_level)

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
    def from_pyproject(cls, toml_key: str = TOML_KEY) -> t.Self:
        return cls.from_pyproject_path(Path.cwd() / "pyproject.toml", toml_key=toml_key)

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
        doc = tomlkit.parse(pyproject_path.read_text())
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
        doc = tomlkit.parse(pyproject_path.read_text())
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
            tomlkit.parse(pyproject_path.read_text())
            if pyproject_path.exists()
            else tomlkit.document()
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
