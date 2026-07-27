import textwrap
import datetime as dt
from pathlib import Path

import pytest

from src.vommit.changelog import Changelog
from src.vommit.commits import CommitEntry
from src.vommit.config import ChangelogConfig, Config
from src.vommit.errors import VommitError


def changelog(tmp_path: Path, **settings) -> Changelog:
    return Changelog(settings=ChangelogConfig.load(settings), root=tmp_path)


def entry(type: str, description: str, scope=None, breaking=False) -> CommitEntry:
    return CommitEntry(
        type=type, scope=scope, description=description, breaking=breaking
    )


def test_render_entry_groups_by_type_and_scope(tmp_path):
    rendered = changelog(tmp_path).render_entry(
        "1.2.0",
        [
            entry("feat", "add endpoint", scope="api"),
            entry("fix", "correct a bug"),
            entry("fix", "correct another bug"),
            entry("chore", "not shown"),
        ],
        today=dt.date(2023, 4, 10),
    )

    assert rendered == textwrap.dedent(
        """\
        ## v1.2.0 (2023-04-10)

        ### Feature
        * **api:** add endpoint

        ### Fixes
        * correct a bug
        * correct another bug"""
    )


def test_render_entry_promotes_breaking_changes(tmp_path):
    rendered = changelog(tmp_path).render_entry(
        "2.0.0",
        [
            entry("feat", "drop python 3.12", breaking=True),
            entry("feat", "add endpoint"),
        ],
        today=dt.date(2023, 4, 10),
    )

    assert rendered == textwrap.dedent(
        """\
        ## v2.0.0 (2023-04-10)

        ### Breaking Change
        * drop python 3.12

        ### Feature
        * add endpoint"""
    )


def test_render_entry_keeps_breaking_under_its_type_when_not_configured(tmp_path):
    # a `levels` mapping without a `break` entry should still show breaking
    # commits, not silently drop them.
    # (loading from toml deep-merges the defaults, so set it outright)
    log = changelog(tmp_path)
    log.settings.levels = {"feat": "Feature{s}"}
    rendered = log.render_entry(
        "2.0.0", [entry("feat", "drop python 3.12", breaking=True)]
    )

    assert "### Feature" in rendered
    assert "drop python 3.12" in rendered


def test_render_entry_without_matching_commits_is_title_only(tmp_path):
    rendered = changelog(tmp_path).render_entry(
        "1.2.0", [entry("chore", "nothing relevant")], today=dt.date(2023, 4, 10)
    )
    assert rendered == "## v1.2.0 (2023-04-10)"


def test_insert_keeps_marker_and_puts_entry_below(tmp_path):
    log = changelog(tmp_path)
    content = f"# Changelog\n\n{log.settings.placeholder}\n\n## v0.1.0 (2023-01-01)\n"

    updated = log.insert(content, "## v0.2.0 (2023-04-10)")

    assert updated == (
        "# Changelog\n\n"
        f"{log.settings.placeholder}\n\n"
        "## v0.2.0 (2023-04-10)\n\n"
        "## v0.1.0 (2023-01-01)\n"
    )


def test_insert_survives_regex_metacharacters_in_commit_messages(tmp_path):
    # a commit message is data, not a re.sub replacement template
    log = changelog(tmp_path)
    rendered = log.render_entry("1.0.0", [entry("fix", r"escape \g<0> and \1")])

    updated = log.insert(log.initial_content(), rendered)

    assert r"* escape \g<0> and \1" in updated


def test_insert_without_placeholder_raises(tmp_path):
    with pytest.raises(VommitError, match="placeholder"):
        changelog(tmp_path).insert("# Changelog\n\nnothing here\n", "## v1.0.0")


def test_auto_regex_matches_its_own_default_placeholder(tmp_path):
    # regression: the derived regex used to double-escape the whitespace in the
    # default placeholder, so it never matched the marker it came from.
    log = changelog(tmp_path)
    assert log.settings.placeholder_re.search(log.initial_content())


def test_read_does_not_create_the_file(tmp_path):
    log = changelog(tmp_path)

    assert log.read() == log.initial_content()
    assert not log.path.exists()


def test_ensure_creates_once_and_then_leaves_it_alone(tmp_path):
    log = changelog(tmp_path, file="docs/CHANGELOG.md")

    created = log.ensure()
    assert created.read_text() == log.initial_content()

    created.write_text("hand written\n")
    assert log.ensure().read_text() == "hand written\n"


def test_plan_does_not_touch_the_filesystem_until_applied(tmp_path):
    log = changelog(tmp_path)
    update = log.plan("1.0.0", [entry("feat", "something")], today=dt.date(2023, 4, 10))

    assert not log.path.exists()
    assert "## v1.0.0 (2023-04-10)" in update.entry

    update.apply()
    assert "## v1.0.0 (2023-04-10)" in log.path.read_text()


def test_plan_fails_before_writing_when_the_placeholder_is_missing(tmp_path):
    log = changelog(tmp_path)
    log.path.write_text("# Changelog\n\nno marker\n")

    with pytest.raises(VommitError):
        log.plan("1.0.0", [entry("feat", "something")])

    assert log.path.read_text() == "# Changelog\n\nno marker\n"


def test_absolute_changelog_path_is_used_as_is(tmp_path):
    target = tmp_path / "elsewhere" / "NOTES.md"
    log = changelog(tmp_path / "project", file=str(target))
    assert log.path == target


def test_commit_entries_feed_the_renderer(tmp_path):
    config = Config.default()
    entries = config.commit_entries(
        [
            "feat(api): add endpoint",
            "fix!: drop the old flag",
            "chore: ignored by levels",
            "not a commit header",
            "docs:",
        ]
    )

    rendered = Changelog(settings=config.changelog, root=tmp_path).render_entry(
        "1.0.0", entries, today=dt.date(2023, 4, 10)
    )

    assert rendered == textwrap.dedent(
        """\
        ## v1.0.0 (2023-04-10)

        ### Breaking Change
        * drop the old flag

        ### Feature
        * **api:** add endpoint"""
    )


def test_remove_takes_out_one_entry(tmp_path):
    log = changelog(tmp_path)
    content = (
        "# Changelog\n\n"
        "<!-- next-version-placeholder -->\n\n"
        "## v0.2.0 (2023-04-10)\n\n"
        "### Feature\n"
        "* new thing\n\n"
        "## v0.1.1 (2023-01-01)\n\n"
        "### Fix\n"
        "* old thing\n"
    )

    assert log.remove(content, "0.2.0") == (
        "# Changelog\n\n"
        "<!-- next-version-placeholder -->\n\n"
        "## v0.1.1 (2023-01-01)\n\n"
        "### Fix\n"
        "* old thing\n"
    )


def test_remove_the_only_entry_restores_a_fresh_changelog(tmp_path):
    log = changelog(tmp_path)
    content = log.insert(
        log.initial_content(), log.render_entry("0.2.0", [], dt.date(2023, 4, 10))
    )

    assert log.remove(content, "0.2.0") == log.initial_content()


def test_remove_undoes_exactly_what_insert_did(tmp_path):
    log = changelog(tmp_path)
    before = (
        "# Changelog\n\n"
        "<!-- next-version-placeholder -->\n\n"
        "## v0.1.1 (2023-01-01)\n\n"
        "### Fix\n"
        "* old thing\n"
    )
    added = log.render_entry("0.2.0", [entry("feat", "new thing")], dt.date(2023, 4, 10))

    assert log.remove(log.insert(before, added), "0.2.0") == before


def test_remove_ignores_a_version_that_is_not_listed(tmp_path):
    log = changelog(tmp_path)

    assert log.remove(log.initial_content(), "0.2.0") is None


def test_remove_matches_whatever_date_the_entry_carries(tmp_path):
    log = changelog(tmp_path)
    content = log.insert(
        log.initial_content(), log.render_entry("0.2.0", [], dt.date(1999, 12, 31))
    )

    assert log.remove(content, "0.2.0") == log.initial_content()
