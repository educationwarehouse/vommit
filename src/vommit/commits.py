import dataclasses as dc
import re
import typing as t

VersionBump = t.Literal["major", "minor", "patch"]

# pseudo change type: not a conventional-commit type, but a group a commit of
# any type can land in once it is marked as breaking.
BREAKING: t.Final = "break"

BUMP_PRIORITY: dict[VersionBump, int] = {"patch": 1, "minor": 2, "major": 3}

RE_COMMIT_HEADER = re.compile(
    r"^(?P<type>[A-Za-z][\w-]*)(?P<bang_before_scope>!)?"
    r"(?:\((?P<scope>[^)]+)\))?(?P<bang_after_scope>!)?:",
)
RE_BREAKING_CHANGE_FOOTER = re.compile(r"(?im)^(?:BREAKING[ -]CHANGE):\s+.+$")


@dc.dataclass(frozen=True)
class CommitHeader:
    """
    The parsed first line of a conventional commit.

    Carries the raw breaking-change signals rather than a verdict; whether they
    count is a policy question, answered by `Config`.
    """

    type: str
    scope: str | None
    description: str
    bang: bool


@dc.dataclass(frozen=True)
class CommitEntry:
    """
    A commit as the changelog cares about it.
    """

    type: str
    scope: str | None
    description: str
    breaking: bool


def parse_commit(message: str) -> CommitHeader | None:
    """
    Parse the header of a conventional commit; None if it is not one.
    """
    header = first_line(message)
    match = RE_COMMIT_HEADER.match(header)
    if not match:
        return None

    return CommitHeader(
        type=match.group("type").lower(),
        scope=match.group("scope"),
        description=header[match.end() :].strip(),
        bang=bool(match.group("bang_before_scope") or match.group("bang_after_scope")),
    )


def first_line(message: str) -> str:
    stripped = message.strip()
    return stripped.splitlines()[0] if stripped else ""


def has_breaking_footer(message: str) -> bool:
    return bool(RE_BREAKING_CHANGE_FOOTER.search(message.strip()))


def highest_version_bump(bumps: t.Iterable[VersionBump | None]) -> VersionBump | None:
    """
    Given a collection of (optional) version bumps, return the most significant one.
    """
    highest: VersionBump | None = None
    for bump in bumps:
        if bump and (highest is None or BUMP_PRIORITY[bump] > BUMP_PRIORITY[highest]):
            highest = bump
    return highest


def split_commit_log(raw: str) -> list[str]:
    """
    Split the NUL-separated records of `git log -z --format=%B`.
    """
    return [message.strip() for message in raw.split("\0") if message.strip()]
