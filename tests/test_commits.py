from src.vommit.commits import (
    CommitHeader,
    first_line,
    has_breaking_footer,
    highest_version_bump,
    parse_commit,
    split_commit_log,
)


def test_parse_commit_plain():
    assert parse_commit("fix: correct a bug") == CommitHeader(
        type="fix", scope=None, description="correct a bug", bang=False
    )


def test_parse_commit_scope_is_kept_and_type_lowercased():
    assert parse_commit("FEAT(api): add endpoint") == CommitHeader(
        type="feat", scope="api", description="add endpoint", bang=False
    )


def test_parse_commit_bang_before_and_after_scope():
    assert parse_commit("feat!(api): boom").bang is True
    assert parse_commit("feat(api)!: boom").bang is True
    assert parse_commit("feat(api): calm").bang is False


def test_parse_commit_only_reads_the_header():
    header = parse_commit("feat(scope)!: breaking\n\nBREAKING CHANGE: whoops")
    assert header.description == "breaking"


def test_parse_commit_rejects_non_conventional():
    assert parse_commit("not a conventional commit") is None
    assert parse_commit("") is None
    assert parse_commit("   \n\n  ") is None


def test_first_line():
    assert first_line("  one\ntwo\n") == "one"
    assert first_line("") == ""


def test_has_breaking_footer():
    assert has_breaking_footer("fix: x\n\nBREAKING CHANGE: gone") is True
    assert has_breaking_footer("fix: x\n\nBREAKING-CHANGE: gone") is True
    assert has_breaking_footer("fix: x\n\nbreaking change: gone") is True
    assert has_breaking_footer("fix: x\n\nBREAKING CHANGE:") is False
    assert has_breaking_footer("fix: mentions BREAKING CHANGE: inline") is False


def test_highest_version_bump():
    assert highest_version_bump(["patch", "minor", None]) == "minor"
    assert highest_version_bump(["patch", "major", "minor"]) == "major"
    assert highest_version_bump(["patch"]) == "patch"
    assert highest_version_bump([None, None]) is None
    assert highest_version_bump([]) is None


def test_split_commit_log():
    assert split_commit_log("feat: a\n\0fix: b\n\0") == ["feat: a", "fix: b"]
    assert split_commit_log("") == []
