import tempfile
import textwrap
from datetime import date
from pathlib import Path

import pytest

from src.vommit.config import (
    DERIVE_BRANCH,
    ChangelogConfig,
    CommandConfig,
    Config,
    GitConfig,
    PypiConfig,
    _to_plain_mapping,
)
from src.vommit.helpers import throw


def test_throw():
    with pytest.raises(ValueError):
        throw(ValueError(":)"))
        assert False


def test_config_pyproject_reads_the_git_section(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent("""
        [tool.vommit]

        [tool.vommit.git]
        origin = 'upstream'
        branch = 'myproject/staging'
        """)
    )

    config = Config.from_pyproject(tmp_path)

    assert config.git.origin == "upstream"
    assert config.git.branch == "myproject/staging"


def test_config_pyproject_falls_back_to_defaults(tmp_path):
    assert Config.from_pyproject(tmp_path) == Config.default()


def test_branch_placeholder_survives_loading(tmp_path):
    # regression: branch detection used to run in __post_init__, which never
    # fires for a nested section, so the placeholder leaked out as-is. It is
    # now resolved on use (GitRepo.resolve_branch) and stays verbatim here.
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.vommit.git]\nbranch = "{DERIVE_BRANCH}"\n'
    )

    assert Config.from_pyproject(tmp_path).git.branch == DERIVE_BRANCH


def test_placeholder_regex_is_available_on_a_nested_config(tmp_path):
    # regression: _placeholder_re was built in __post_init__, so any config
    # loaded from pyproject.toml raised AttributeError on first use.
    (tmp_path / "pyproject.toml").write_text(
        "[tool.vommit.changelog]\nenabled = true\n"
    )

    config = Config.from_pyproject(tmp_path)

    assert config.changelog.placeholder_re.search(config.changelog.placeholder)


def test_placeholder_regex_override_wins():
    config = ChangelogConfig.load({"placeholder_regex": r"^<!--\s*marker\s*-->$"})
    assert config.effective_placeholder_regex == r"^<!--\s*marker\s*-->$"
    assert config.placeholder_re.search("<!--  marker  -->")


def test_build_placeholder_regex_tolerates_extra_spacing():
    pattern = ChangelogConfig.build_placeholder_regex("<!-- marker -->")
    assert ChangelogConfig.load({"placeholder_regex": pattern}).placeholder_re.search(
        "<!--   marker\t-->"
    )


def test_active_sections_treat_absent_and_disabled_alike():
    config = Config.default()
    assert config.active_git is config.git
    assert config.active_changelog is config.changelog
    assert config.active_pypi is config.pypi

    config.git.enabled = False
    config.changelog = None
    config.pypi.enabled = False

    assert config.active_git is None
    assert config.active_changelog is None
    assert config.active_pypi is None


def test_pluralize_templates():
    config = ChangelogConfig.default()

    assert config.pluralize("feat", 1) == "Feature"
    assert config.pluralize("feat", 2) == "Features"

    assert config.pluralize("fix", 1) == "Bug Fix"
    assert config.pluralize("fix", 2) == "Bug Fixes"

    assert config.pluralize("perf", 1) == "Performance"
    assert config.pluralize("docs", 2) == "Documentation"


def test_git_tag_format_template():
    config = GitConfig.load({"tag_format": "v{version}"})

    assert config.format_tag(version="1.2.3") == "v1.2.3"


def test_git_commit_format_template():
    config = GitConfig.load({"commit_format": "Release `{version}`;"})

    assert config.format_commit(version="1.2.3") == "Release `1.2.3`;"


def test_git_formats_are_skippable():
    # the annotation says "empty to disable"; that must not leak an empty string
    config = GitConfig.load({"tag_format": "", "commit_format": ""})

    assert config.format_tag("1.2.3") is None
    assert config.format_commit("1.2.3") is None
    assert config.tag_glob is None


def test_git_tag_glob():
    assert GitConfig.load({"tag_format": "v{version}"}).tag_glob == "v*"
    assert GitConfig.load({"tag_format": "rel-{version}-x"}).tag_glob == "rel-*-x"


def test_changelog_resolve_path(tmp_path):
    config = ChangelogConfig.default()
    assert config.resolve_path(tmp_path) == tmp_path / "CHANGELOG.md"

    absolute = tmp_path / "elsewhere.md"
    assert (
        ChangelogConfig.load({"file": str(absolute)}).resolve_path("/nope") == absolute
    )


def test_changelog_entry_title_format_default():
    config = ChangelogConfig.default()
    assert (
        config.format_entry_title(version="1.2.3", date=date(2023, 4, 10))
        == "## v1.2.3 (2023-04-10)"
    )


def test_changelog_entry_title_format_custom_date_spec():
    config = ChangelogConfig.load(
        {"entry_title_format": "## release {version} [{date:%d/%m/%Y}]"},
    )
    assert (
        config.format_entry_title(version="1.2.3", date=date(2023, 4, 10))
        == "## release 1.2.3 [10/04/2023]"
    )


def test_changelog_entry_title_defaults_to_today():
    config = ChangelogConfig.default()
    assert str(date.today()) in config.format_entry_title("1.2.3")


def test_pypi_defaults():
    config = PypiConfig.default()
    assert config.username == "__token__"
    assert config.use_keyring is True


def test_commands_release_template():
    config = CommandConfig.load(
        {
            "clean": "rm -rf dist",
            "build": "python -m build",
            "publish": "twine upload dist/*",
            "release": "{clean} ; {build} ; {publish}",
        },
    )

    assert (
        config.release_command == "rm -rf dist ; python -m build ; twine upload dist/*"
    )


def test_version_bump_map_config():
    config = Config.load(
        {
            "allow_breaking_bang": True,
            "allow_breaking_footer": True,
            "version_bump_map": {
                "feat": "minor",
                "fix": "patch",
                "chore": "patch",
                "breaky": "major",
            },
        },
    )

    assert config.resolve_version_bump_from_commit("fix: something happened") == "patch"
    assert (
        config.resolve_version_bump_from_commit("feat(scope): add feature") == "minor"
    )
    assert (
        config.resolve_version_bump_from_commit("breaky(everything): custom one")
        == "major"
    )
    assert (
        config.resolve_version_bump_from_commit("feat!(scope): breaking change")
        == "major"
    )
    assert config.resolve_version_bump_from_commit("feat!: breaking change") == "major"

    assert (
        config.resolve_version_bump_from_commit(
            "feat(project): another breaking change\n\nBREAKING CHANGE: something else changed",
        )
        == "major"
    )
    assert config.resolve_version_bump_from_commit("refactor: reformat code") is None
    assert config.resolve_version_bump_from_commit("first commit") is None


def test_breaking_change_policy_can_be_switched_off():
    config = Config.load(
        {"allow_breaking_bang": False, "allow_breaking_footer": False},
    )

    assert config.resolve_version_bump_from_commit("feat!: breaking") == "minor"
    assert (
        config.resolve_version_bump_from_commit("fix: x\n\nBREAKING CHANGE: gone")
        == "patch"
    )


def test_breaking_footer_counts_without_a_conventional_header():
    config = Config.default()

    assert (
        config.resolve_version_bump_from_commit("whoops\n\nBREAKING CHANGE: gone")
        == "major"
    )


def test_commit_entries_skips_unparseable_and_empty_descriptions():
    entries = Config.default().commit_entries(
        ["feat(api): add endpoint", "fix!: boom", "nonsense", "docs:"]
    )

    assert [(e.type, e.scope, e.description, e.breaking) for e in entries] == [
        ("feat", "api", "add endpoint", False),
        ("fix", None, "boom", True),
    ]


def test_write_to_pyproject_comments_intact():
    some_config = textwrap.dedent(
        """
        [tool.vommit]
        # this is config for the vommit release tool
        git = false # shorthand for tool.vommit.git.enabled = false
        """
    )

    with tempfile.TemporaryDirectory() as d:
        pyproject = Path(d) / "pyproject.toml"
        pyproject.write_text(some_config)

        config = Config.from_pyproject_path(pyproject)
        config.write_to_pyproject(pyproject)

        pyproject_contents = pyproject.read_text()

    assert "# this is config for the vommit release tool" in pyproject_contents
    assert (
        "git = false # shorthand for tool.vommit.git.enabled = false"
        in pyproject_contents
    )


def test_write_to_pyproject_keeps_derived_state_out_of_the_file(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    config = Config.default()
    # touch the lazy property so its cache exists before writing
    assert config.changelog.placeholder_re

    config.write_to_pyproject(pyproject)

    assert "_placeholder_re" not in pyproject.read_text()


def test_to_plain_mapping_unwraps_nested_containers():
    config = GitConfig.load({"origin": "upstream"})

    assert _to_plain_mapping({"a": [config, ("x", 1)]})["a"] == [
        _to_plain_mapping(config),
        ["x", 1],
    ]


def test_write_to_pyproject_collapses_disabled_sections(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    config = Config.default()
    config.pypi.enabled = False
    config.version_bump_map = {"feat": "minor"}

    config.write_to_pyproject(pyproject)
    written = pyproject.read_text()

    assert "[tool.vommit.pypi]\nenabled = false" in written
    assert "use_keyring" not in written
    assert Config.from_pyproject_path(pyproject).pypi.enabled is False


def test_config_introspection_helpers(tmp_path):
    pyproject = tmp_path / "pyproject.toml"

    assert Config.has_pyproject_config(pyproject) is False
    assert Config.configured_key_paths(pyproject) == set()
    assert Config.is_complete(pyproject) is False

    pyproject.write_text("[project]\nname = 'x'\n")
    assert Config.has_pyproject_config(pyproject) is False
    assert Config.configured_key_paths(pyproject) == set()

    pyproject.write_text("[tool.vommit.git]\norigin = 'upstream'\n")
    assert Config.has_pyproject_config(pyproject) is True
    assert Config.configured_key_paths(pyproject) == {"git.origin"}
    assert Config.is_complete(pyproject) is False

    Config.default().write_to_pyproject(pyproject)
    assert Config.is_complete(pyproject) is True
