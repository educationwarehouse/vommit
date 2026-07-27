import typing as t
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .commits import BREAKING, CommitEntry
from .config import ChangelogConfig
from .errors import VommitError


@dataclass(frozen=True)
class ChangelogUpdate:
    """
    A pending changelog write: what the file should contain, but not written yet.

    Computing this before anything is mutated means a missing placeholder is
    caught while the project is still in one piece.
    """

    path: Path
    entry: str
    content: str

    def apply(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.content)
        return self.path


@dataclass(frozen=True)
class Changelog:
    """
    Renders and updates the changelog file described by `ChangelogConfig`.
    """

    settings: ChangelogConfig
    root: Path

    @property
    def path(self) -> Path:
        return self.settings.resolve_path(self.root)

    def initial_content(self) -> str:
        return f"# Changelog\n\n{self.settings.placeholder}\n"

    def read(self) -> str:
        """
        Current contents, or what a fresh changelog would look like.

        Deliberately does not create the file: a dry run must leave no trace.
        """
        path = self.path
        return path.read_text() if path.exists() else self.initial_content()

    def ensure(self) -> Path:
        path = self.path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.initial_content())
        return path

    def group_of(self, entry: CommitEntry) -> str | None:
        """
        The configured level a commit belongs to, or None if it is not listed.

        Breaking commits only get their own group when one is configured; older
        configs without a `break` level keep them under their own type.
        """
        if entry.breaking and BREAKING in self.settings.levels:
            return BREAKING
        return entry.type if entry.type in self.settings.levels else None

    def render_entry(
        self,
        version: str,
        commits: t.Iterable[CommitEntry],
        today: date | None = None,
    ) -> str:
        """
        Build a changelog block for `version`, grouping commits by level.
        Commits whose type is not configured in `levels` are omitted.
        """
        groups: dict[str, list[str]] = {level: [] for level in self.settings.levels}
        for commit in commits:
            group = self.group_of(commit)
            if group is None:
                continue

            description = commit.description
            groups[group].append(
                f"**{commit.scope}:** {description}" if commit.scope else description
            )

        lines = [self.settings.format_entry_title(version, today)]
        for level, entries in groups.items():
            if not entries:
                continue
            lines.append("")
            lines.append(f"### {self.settings.pluralize(level, len(entries))}")
            lines.extend(f"* {entry}" for entry in entries)

        return "\n".join(lines)

    def insert(self, content: str, entry: str) -> str:
        """
        Put `entry` directly below the placeholder, keeping the placeholder itself.
        """
        placeholder_re = self.settings.placeholder_re
        match = placeholder_re.search(content)
        if not match:
            raise VommitError(
                f"Could not find the changelog placeholder in '{self.path}'. "
                f"Expected a line matching: {self.settings.effective_placeholder_regex}"
            )

        replacement = f"{match.group(0)}\n\n{entry.strip('\n')}"
        # a plain replacement string would interpret backslashes and \1 group
        # references appearing in commit messages.
        return placeholder_re.sub(lambda _: replacement, content, count=1)

    def remove(self, content: str, version: str) -> str | None:
        """
        `content` without `version`'s entry; None when it has no such entry.

        The inverse of `insert`, down to the blank line it leaves behind the
        placeholder. Only the one entry is touched, so hand-written notes
        elsewhere in the file survive being undone.
        """
        match = self.settings.entry_title_re(version).search(content)
        if not match:
            return None

        # the next entry of any version bounds this one; otherwise it runs to EOF
        following = self.settings.entry_title_re().search(content, match.end())
        before = content[: match.start()].rstrip("\n")
        rest = content[following.start() :] if following else ""
        return f"{before}\n\n{rest}" if rest else f"{before}\n"

    def plan(
        self,
        version: str,
        commits: t.Iterable[CommitEntry],
        today: date | None = None,
    ) -> ChangelogUpdate:
        entry = self.render_entry(version, commits, today)
        return ChangelogUpdate(
            path=self.path,
            entry=entry,
            content=self.insert(self.read(), entry),
        )
