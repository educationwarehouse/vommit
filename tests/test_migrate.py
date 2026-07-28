import textwrap
from pathlib import Path

import pytest
import tomlkit

from src.vommit.commits import BREAKING
from src.vommit.config import DERIVE_BRANCH, Config
from src.vommit.errors import VommitError
from src.vommit.migrate import (
    METADATA_IMPORT,
    MigrationReport,
    Note,
    Raw,
    VersionFile,
    apply_static_version,
    parse_unsupported_policy,
    plan_static_version,
    plan_version_migration,
    project_name,
    read_psr,
    rewrite_version_files,
    split_fields,
    strip_psr,
    translate,
)
from src.vommit.shell import LocalRunner
from src.vommit.versioning import UvProject

# modelled on a real v7 project: hatchling + dynamic version, six keys
ENDOW = """
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "endow"
dynamic = ["version"]

[project.optional-dependencies]
dev = ["pytest", "python-semantic-release<8"]

[tool.hatch.version]
path = "src/endow/__about__.py"

[tool.semantic_release]
branch = "master"
version_variable = "src/endow/__about__.py:__version__"
change_log = "CHANGELOG.md"
upload_to_repository = false
upload_to_release = false
build_command = "hatch build"
"""

ABOUT = '"""Package metadata for endow."""\n\n__version__ = "0.1.1"\n'

ENDOW_VERSION_FILE = VersionFile("src/endow/__about__.py", "__version__")


def endow(tmp_path: Path) -> Path:
    """
    endow as it stands today: hatchling, dynamic version, v7 config.
    """
    pyproject = write(tmp_path, ENDOW)
    about = tmp_path / ENDOW_VERSION_FILE.path
    about.parent.mkdir(parents=True)
    about.write_text(ABOUT)
    return pyproject


def write(tmp_path: Path, body: str) -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(textwrap.dedent(body))
    return pyproject


def psr(tmp_path: Path, body: str = "") -> Raw:
    raw = read_psr(write(tmp_path, f"[tool.semantic_release]\n{textwrap.dedent(body)}"))
    assert raw is not None
    return raw


def notes(entries: list[Note]) -> dict[str, str]:
    return {note.key: note.detail for note in entries}


def test_read_psr_without_a_project(tmp_path):
    assert read_psr(tmp_path / "pyproject.toml") is None


def test_read_psr_without_a_semantic_release_table(tmp_path):
    assert read_psr(write(tmp_path, "[project]\nname = 'x'\n")) is None
    assert read_psr(write(tmp_path, "[tool.other]\nkey = 1\n")) is None


def test_read_psr_overlays_v7_defaults(tmp_path):
    raw = psr(tmp_path, "branch = 'trunk'\n")

    assert raw.get("branch") == "trunk"
    assert raw.is_explicit("branch")

    # unset, but v7 still applied it
    assert raw.get("changelog_placeholder") == "<!--next-version-placeholder-->"
    assert not raw.is_explicit("changelog_placeholder")


def test_read_psr_keeps_plain_python_types(tmp_path):
    raw = psr(
        tmp_path,
        """
        remove_dist = false
        changelog_sections = ["feature", "fix"]
        commit_subject = "v{version}"
        parser_angular_default_level_bump = 0
        """,
    )

    assert raw.get("remove_dist") is False
    assert raw.get("changelog_sections") == ["feature", "fix"]
    assert raw.get("commit_subject") == "v{version}"
    assert raw.get("parser_angular_default_level_bump") == 0


def test_an_empty_v7_config_still_translates_v7_behaviour(tmp_path):
    """
    The whole point of the defaults overlay: a project that configured nothing
    was still released under v7's defaults, not vommit's.
    """
    migration = translate(psr(tmp_path))
    config = migration.config

    assert config.changelog.placeholder == "<!--next-version-placeholder-->"
    assert config.prerelease_token == "beta"  # vommit would default to rc
    assert config.git.tag_format == "v{version}"
    assert config.git.commit_format == "{version}"

    # v7's default parser did not release on docs; vommit's default map does
    assert "docs" not in config.version_bump_map
    assert config.version_bump_map == {
        BREAKING: "major",
        "feat": "minor",
        "fix": "patch",
        "perf": "patch",
    }


def test_an_empty_v7_config_translates_the_default_sections(tmp_path):
    config = translate(psr(tmp_path)).config

    assert config.changelog.levels == {
        "feat": "Feature{s}",
        "fix": "Fix{es}",
        BREAKING: "Breaking Change{s}",
        "docs": "Documentation",
        "perf": "Performance",
    }


def test_an_unwritten_branch_is_left_to_auto_detect(tmp_path):
    """
    v7's `branch = master` default was v7's guess, not the project's decision;
    pinning it breaks the first bump on every repo that renamed since.
    """
    migration = translate(psr(tmp_path))

    assert migration.config.git.branch == DERIVE_BRANCH
    assert "git.branch" not in migration.key_paths
    assert "master" in notes(migration.report.lossy)["branch"]
    assert DERIVE_BRANCH in notes(migration.report.lossy)["branch"]


def test_a_branch_the_project_wrote_down_is_pinned(tmp_path):
    migration = translate(psr(tmp_path, "branch = 'trunk'\n"))

    assert migration.config.git.branch == "trunk"
    assert "git.branch" in migration.key_paths
    assert "branch" not in notes(migration.report.lossy)


def test_the_dropped_bump_types_point_at_the_override(tmp_path):
    report = translate(psr(tmp_path)).report

    assert "docs" in notes(report.lossy)["parser_angular_*"]
    assert "vommit bump --patch" in notes(report.lossy)["parser_angular_*"]


def test_endow(tmp_path):
    write(tmp_path, ENDOW)
    raw = read_psr(tmp_path / "pyproject.toml")
    assert raw is not None

    migration = translate(raw)
    config = migration.config

    assert config.git.branch == "master"
    assert config.commands.build == "hatch build"
    assert config.pypi.enabled is False
    assert migration.version_files == [
        VersionFile(path="src/endow/__about__.py", variable="__version__")
    ]

    unsupported = notes(migration.report.unsupported)
    # the typo v7 ignored for the project's whole life
    assert "Did you mean changelog_file?" in unsupported["change_log"]
    assert "forge releases" in unsupported["upload_to_release"]


def test_endow_key_paths_feed_the_interactive_flow(tmp_path):
    write(tmp_path, ENDOW)
    raw = read_psr(tmp_path / "pyproject.toml")
    assert raw is not None

    key_paths = translate(raw).key_paths

    assert {"git.branch", "commands.build", "pypi.enabled"} <= key_paths
    assert key_paths <= Config.interactive_key_paths()


def test_a_non_angular_parser_is_refused(tmp_path):
    raw = psr(tmp_path, "commit_parser = 'semantic_release.history.emoji_parser'\n")

    with pytest.raises(VommitError, match="conventional commits"):
        translate(raw)


def test_a_version_pattern_is_refused(tmp_path):
    raw = psr(tmp_path, "version_pattern = 'README.md:version-{version}'\n")

    with pytest.raises(VommitError, match="version_pattern"):
        translate(raw)


def test_a_poetry_version_is_refused(tmp_path):
    raw = psr(tmp_path, "version_toml = 'pyproject.toml:tool.poetry.version'\n")

    with pytest.raises(VommitError, match=r"\[project\].version"):
        translate(raw)


def test_a_project_version_toml_is_accepted(tmp_path):
    raw = psr(tmp_path, "version_toml = 'pyproject.toml:project.version'\n")

    assert "version_toml" in notes(translate(raw).report.mapped)


def test_tag_commit_off_clears_the_tag_format(tmp_path):
    migration = translate(psr(tmp_path, "tag_commit = false\n"))

    assert migration.config.git.tag_format is None
    assert migration.config.git.format_tag("1.2.3") is None
    assert "tagging stays off" in notes(migration.report.mapped)["tag_commit"]


def test_dist_path_becomes_the_clean_command(tmp_path):
    assert translate(psr(tmp_path)).config.commands.clean == "rm -r ./dist"
    assert (
        translate(psr(tmp_path, "dist_path = 'build/out/'\n")).config.commands.clean
        == "rm -r ./build/out"
    )


def test_remove_dist_off_drops_the_clean_step(tmp_path):
    config = translate(psr(tmp_path, "remove_dist = false\n")).config

    assert "{clean}" not in config.commands.release
    assert config.commands.release_command == "uv build && uv publish"


def test_either_upload_switch_disables_publishing(tmp_path):
    assert translate(psr(tmp_path)).config.pypi.enabled is True
    assert (
        translate(psr(tmp_path, "upload_to_pypi = false\n")).config.pypi.enabled
        is False
    )
    assert (
        translate(psr(tmp_path, "upload_to_repository = false\n")).config.pypi.enabled
        is False
    )


def test_build_command_is_only_taken_when_it_was_written_down(tmp_path):
    # v7's default is `python setup.py sdist bdist_wheel`; carrying that over
    # would replace vommit's `uv build` with legacy tooling
    assert translate(psr(tmp_path)).config.commands.build == "uv build"
    assert (
        translate(
            psr(tmp_path, "build_command = 'hatch build'\n")
        ).config.commands.build
        == "hatch build"
    )


def test_changelog_sections_are_translated_by_long_name(tmp_path):
    config = translate(
        psr(
            tmp_path,
            "changelog_sections = 'breaking,feature,fix,performance,documentation,refactor'\n",
        )
    ).config

    assert list(config.changelog.levels) == [
        BREAKING,
        "feat",
        "fix",
        "perf",
        "docs",
        "refactor",
    ]
    assert config.changelog.levels["refactor"] == "Refactor{s}"
    assert config.changelog.pluralize("refactor", 2) == "Refactors"


def test_an_unknown_section_gets_a_generated_title(tmp_path):
    config = translate(psr(tmp_path, "changelog_sections = 'feature,deps'\n")).config

    assert config.changelog.levels["deps"] == "Deps{s}"


def test_emoji_and_catch_all_sections_are_dropped(tmp_path):
    migration = translate(
        psr(tmp_path, "changelog_sections = 'feature,:sparkles:,Other'\n")
    )

    assert list(migration.config.changelog.levels) == ["feat"]
    dropped = notes(migration.report.lossy)["changelog_sections"]
    assert ":sparkles:" in dropped
    assert "Other" in dropped


def test_untranslatable_sections_keep_the_vommit_defaults(tmp_path):
    migration = translate(psr(tmp_path, "changelog_sections = ':boom:,:zap:'\n"))

    assert migration.config.changelog.levels == Config.default().changelog.levels
    assert (
        "keeping vommit's defaults"
        in notes(migration.report.lossy)["changelog_sections"]
    )


def test_default_sections_do_not_warn(tmp_path):
    # the emoji half of v7's default is not worth a warning nobody can act on
    assert "changelog_sections" not in notes(translate(psr(tmp_path)).report.lossy)


def test_bump_map_from_the_angular_parser_settings(tmp_path):
    config = translate(
        psr(
            tmp_path,
            """
            parser_angular_allowed_types = 'feat,fix,perf,docs,chore'
            parser_angular_minor_types = 'feat,perf'
            parser_angular_patch_types = 'fix'
            """,
        )
    ).config

    # docs and chore are allowed types in no list, and the default bump is
    # no-release, so they stay out of the map entirely
    assert config.version_bump_map == {
        BREAKING: "major",
        "feat": "minor",
        "perf": "minor",
        "fix": "patch",
    }


def test_types_outside_the_lists_follow_the_default_bump(tmp_path):
    config = translate(
        psr(
            tmp_path,
            """
            parser_angular_allowed_types = 'feat,fix,chore'
            parser_angular_minor_types = 'feat'
            parser_angular_patch_types = 'fix'
            parser_angular_default_level_bump = 'patch'
            """,
        )
    ).config

    assert config.version_bump_map == {
        BREAKING: "major",
        "feat": "minor",
        "fix": "patch",
        "chore": "patch",
    }


def test_no_release_types_stay_out_of_the_map(tmp_path):
    config = translate(
        psr(
            tmp_path,
            """
            parser_angular_allowed_types = 'feat,chore'
            parser_angular_minor_types = 'feat'
            parser_angular_patch_types = ''
            """,
        )
    ).config

    assert config.version_bump_map == {BREAKING: "major", "feat": "minor"}


def test_an_invalid_default_level_bump_is_refused(tmp_path):
    raw = psr(tmp_path, "parser_angular_default_level_bump = 'huge'\n")

    with pytest.raises(VommitError, match="parser_angular_default_level_bump"):
        translate(raw)


@pytest.mark.parametrize(
    "tag,token",
    [("a", "alpha"), ("alpha", "alpha"), ("b", "beta"), ("rc", "rc"), ("C", "rc")],
)
def test_prerelease_tags_map_onto_pep_440_segments(tmp_path, tag, token):
    config = translate(psr(tmp_path, f"prerelease_tag = '{tag}'\n")).config

    assert config.prerelease_token == token


def test_an_unusable_prerelease_tag_keeps_the_default(tmp_path):
    migration = translate(psr(tmp_path, "prerelease_tag = 'preview'\n"))

    assert migration.config.prerelease_token == "rc"
    assert "'preview'" in notes(migration.report.lossy)["prerelease_tag"]


def test_version_variable_accepts_a_list_and_a_csv_string(tmp_path):
    csv_form = translate(
        psr(tmp_path, "version_variable = 'a.py:__version__,b.py:VERSION'\n")
    )
    list_form = translate(
        psr(tmp_path, "version_variable = ['a.py:__version__', 'b.py:VERSION']\n")
    )

    assert csv_form.version_files == list_form.version_files
    assert csv_form.version_files == [
        VersionFile("a.py", "__version__"),
        VersionFile("b.py", "VERSION"),
    ]


@pytest.mark.parametrize("entry", ["a.py", "  :__version__", "a.py:  "])
def test_a_malformed_version_variable_is_reported(tmp_path, entry):
    migration = translate(psr(tmp_path, f"version_variable = '{entry}'\n"))

    assert migration.version_files == []
    assert "not `path:variable`" in notes(migration.report.lossy)["version_variable"]


def test_lossy_settings_are_reported(tmp_path):
    migration = translate(
        psr(
            tmp_path,
            """
            commit_message = 'Automatically generated by python-semantic-release'
            major_on_zero = false
            patch_without_tag = true
            version_source = 'tag'
            """,
        )
    )

    lossy = notes(migration.report.lossy)
    assert "body is dropped" in lossy["commit_message"]
    assert "below 1.0.0" in lossy["major_on_zero"]
    assert "never trigger a release" in lossy["patch_without_tag"]
    assert "'tag' ignored" in lossy["version_source"]


def test_lossy_settings_left_at_their_v7_default_stay_quiet(tmp_path):
    lossy = notes(translate(psr(tmp_path, "commit_message = ''\n")).report.lossy)

    assert "commit_message" not in lossy
    assert "major_on_zero" not in lossy
    assert "patch_without_tag" not in lossy
    assert "version_source" not in lossy


def test_unsupported_keys_carry_a_reason(tmp_path):
    report = translate(
        psr(tmp_path, "hvcs = 'gitlab'\ncheck_build_status = true\n")
    ).report

    unsupported = notes(report.unsupported)
    assert unsupported["hvcs"] == "vommit does not talk to a forge"
    assert "CI provider" in unsupported["check_build_status"]


def test_defaults_never_show_up_as_unsupported(tmp_path):
    # hvcs defaults to github; nobody needs to hear about a choice they never made
    assert translate(psr(tmp_path)).report.unsupported == []


def test_an_unrecognisable_key_gets_no_suggestion(tmp_path):
    report = translate(psr(tmp_path, "zzzzzzzz = 1\n")).report

    detail = notes(report.unsupported)["zzzzzzzz"]
    assert "ignored it silently" in detail
    assert "Did you mean" not in detail


def test_split_fields():
    assert split_fields(None) == []
    assert split_fields("") == []
    assert split_fields([]) == []
    assert split_fields("a, b ,c") == ["a", "b", "c"]
    assert split_fields(["a", " b "]) == ["a", "b"]
    assert split_fields('"a,b",c') == ["a,b", "c"]


def test_version_file_parse():
    assert VersionFile.parse("a.py:V") == VersionFile("a.py", "V")
    assert VersionFile.parse("a.py") is None
    assert VersionFile.parse(":V") is None


def test_report_helpers():
    clean = MigrationReport(mapped=[Note("branch", "git.branch = 'main'")])
    assert clean.clean
    assert clean.warnings == []
    assert str(clean.mapped[0]) == "branch: git.branch = 'main'"

    noisy = MigrationReport(lossy=[Note("a", "b")], unsupported=[Note("c", "d")])
    assert not noisy.clean
    assert len(noisy.warnings) == 2


SETUPTOOLS = """
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "sample"
dynamic = ["version"]

[tool.setuptools.dynamic]
version = {attr = "sample.__version__"}
"""

UV_BUILD = """
[build-system]
requires = ["uv_build"]
build-backend = "uv_build"

[project]
name = "sample"
version = "1.2.3"
"""


def test_a_static_project_needs_no_transform(tmp_path):
    assert plan_static_version(write(tmp_path, UV_BUILD), []) is None


def test_a_project_that_already_has_a_static_version_is_left_alone(tmp_path):
    body = "[project]\nname = 'x'\nversion = '1.0.0'\n"
    assert plan_static_version(write(tmp_path, body), []) is None
    assert plan_static_version(write(tmp_path, f"{body}dynamic = ['readme']\n"), []) is None


def test_a_project_without_pep_621_metadata_needs_no_transform(tmp_path):
    # nowhere to put a version; inventing a [project] table is not a migration
    assert plan_static_version(write(tmp_path, "[tool.other]\nkey = 1\n"), []) is None


def test_a_project_with_no_version_at_all_gets_one_frozen_in(tmp_path):
    """
    v7 predates PEP 621 and let the version live only in the source file. That
    file is about to be rewritten, so the literal has to land in [project] --
    the case that used to slip through and leave the project versionless.
    """
    pyproject = write(tmp_path, "[project]\nname = 'x'\n")
    (tmp_path / "x.py").write_text('__version__ = "2.0.0"\n')

    transform = plan_static_version(pyproject, [VersionFile("x.py", "__version__")])

    assert transform is not None
    assert transform.version == "2.0.0"
    assert transform.source == "x.py"
    # nothing dynamic to undo, so no backend hook to take out either
    assert transform.backend is None
    assert transform.hook_key is None
    assert transform.steps == [
        'set [project].version = "2.0.0" (read from x.py)',
    ]


def test_a_version_only_freeze_touches_nothing_else(tmp_path):
    pyproject = write(tmp_path, "[project]\nname = 'x'\ndynamic = ['readme']\n")
    transform = plan_static_version(pyproject, [], fallback="3.0.0")
    assert transform is not None

    apply_static_version(pyproject, transform)
    result = tomlkit.parse(pyproject.read_text())

    assert result["project"]["version"] == "3.0.0"
    assert result["project"]["dynamic"] == ["readme"]


def test_endow_needs_the_static_version_transform(tmp_path):
    transform = plan_static_version(endow(tmp_path), [ENDOW_VERSION_FILE])

    assert transform is not None
    assert transform.version == "0.1.1"
    assert transform.backend == "hatchling"
    assert transform.hook_key == "tool.hatch.version"
    assert transform.source == "src/endow/__about__.py"
    assert transform.steps == [
        'set [project].version = "0.1.1" (read from src/endow/__about__.py)',
        'remove "version" from [project].dynamic',
        "remove [tool.hatch.version]",
    ]


def test_setuptools_dynamic_versions_are_recognised(tmp_path):
    pyproject = write(tmp_path, SETUPTOOLS)

    transform = plan_static_version(pyproject, [], fallback="2.0.0")

    assert transform is not None
    assert transform.backend == "setuptools"
    assert transform.hook_key == "tool.setuptools.dynamic.version"
    assert transform.source == "the latest tag"


def test_the_backend_is_detected_from_its_hook_table_too(tmp_path):
    # a project that never declared a build-system, but clearly uses one
    pyproject = write(
        tmp_path,
        """
        [project]
        name = "x"
        dynamic = ["version"]

        [tool.hatch.version]
        path = "x.py"
        """,
    )
    (tmp_path / "x.py").write_text('__version__ = "3.2.1"\n')

    transform = plan_static_version(pyproject, [VersionFile("x.py", "__version__")])

    assert transform is not None
    assert transform.backend == "hatchling"


def test_uv_build_cannot_have_a_dynamic_version(tmp_path):
    pyproject = write(
        tmp_path,
        """
        [build-system]
        build-backend = "uv_build"

        [project]
        name = "x"
        dynamic = ["version"]
        """,
    )

    with pytest.raises(VommitError, match="uv_build"):
        plan_static_version(pyproject, [])


def test_an_unsupported_backend_is_refused(tmp_path):
    pyproject = write(
        tmp_path,
        """
        [build-system]
        build-backend = "flit_core.buildapi"

        [project]
        name = "x"
        dynamic = ["version"]
        """,
    )

    with pytest.raises(VommitError, match="flit_core.buildapi"):
        plan_static_version(pyproject, [])


def test_an_undeclared_backend_is_refused(tmp_path):
    pyproject = write(tmp_path, "[project]\nname = 'x'\ndynamic = ['version']\n")

    with pytest.raises(VommitError, match="unrecognised backend"):
        plan_static_version(pyproject, [])


def test_hatch_vcs_is_refused(tmp_path):
    pyproject = write(
        tmp_path,
        """
        [build-system]
        build-backend = "hatchling.build"

        [project]
        name = "x"
        dynamic = ["version"]

        [tool.hatch.version]
        source = "vcs"
        """,
    )

    with pytest.raises(VommitError, match="'vcs'"):
        plan_static_version(pyproject, [])


def test_the_tag_is_only_a_fallback(tmp_path):
    # the file literal is what v7 was bumping, so it wins over a stale tag
    transform = plan_static_version(
        endow(tmp_path), [ENDOW_VERSION_FILE], fallback="9.9.9"
    )

    assert transform is not None
    assert transform.version == "0.1.1"


@pytest.mark.parametrize("literal", ['__version__ = "not a version"', "# nothing"])
def test_an_unreadable_version_falls_back_to_the_tag(tmp_path, literal):
    endow(tmp_path)
    (tmp_path / ENDOW_VERSION_FILE.path).write_text(f"{literal}\n")

    transform = plan_static_version(
        tmp_path / "pyproject.toml", [ENDOW_VERSION_FILE], fallback="0.4.0"
    )

    assert transform is not None
    assert transform.version == "0.4.0"


def test_a_project_with_no_version_anywhere_is_refused(tmp_path):
    endow(tmp_path)
    (tmp_path / ENDOW_VERSION_FILE.path).unlink()

    with pytest.raises(VommitError, match="no version to freeze"):
        plan_static_version(tmp_path / "pyproject.toml", [ENDOW_VERSION_FILE])


def test_applying_the_transform_to_endow(tmp_path):
    pyproject = endow(tmp_path)
    transform = plan_static_version(pyproject, [ENDOW_VERSION_FILE])
    assert transform is not None

    apply_static_version(pyproject, transform)
    result = tomlkit.parse(pyproject.read_text())

    assert result["project"]["version"] == "0.1.1"
    assert "dynamic" not in result["project"]
    # the empty [tool.hatch] parent goes with it, the rest of [tool] stays
    assert "hatch" not in result["tool"]
    assert "semantic_release" in result["tool"]


def test_the_transform_leaves_other_dynamic_fields_alone(tmp_path):
    pyproject = write(
        tmp_path,
        """
        [build-system]
        build-backend = "hatchling.build"

        [project]
        name = "x"
        dynamic = ["version", "readme"]

        [tool.hatch.version]
        path = "x.py"

        [tool.hatch.build]
        packages = ["x"]
        """,
    )
    (tmp_path / "x.py").write_text('__version__ = "1.0.0"\n')
    transform = plan_static_version(pyproject, [VersionFile("x.py", "__version__")])
    assert transform is not None

    apply_static_version(pyproject, transform)
    result = tomlkit.parse(pyproject.read_text())

    assert result["project"]["dynamic"] == ["readme"]
    # [tool.hatch] still holds build settings, so it is not pruned
    assert "version" not in result["tool"]["hatch"]
    assert "build" in result["tool"]["hatch"]


def test_applying_the_transform_to_a_setuptools_project(tmp_path):
    pyproject = write(tmp_path, SETUPTOOLS)
    transform = plan_static_version(pyproject, [], fallback="2.0.0")
    assert transform is not None

    apply_static_version(pyproject, transform)
    result = tomlkit.parse(pyproject.read_text())

    assert result["project"]["version"] == "2.0.0"
    assert "setuptools" not in result.get("tool", {})


def test_uv_can_only_bump_the_transformed_project(tmp_path):
    """
    The point of the whole transform, against the real `uv`: it refuses a
    dynamic project outright, whatever the build backend.
    """
    pyproject = endow(tmp_path)
    uv = UvProject(runner=LocalRunner(), root=tmp_path)

    with pytest.raises(VommitError, match="uv version"):
        uv.preview_bump("patch")

    transform = plan_static_version(pyproject, [ENDOW_VERSION_FILE])
    assert transform is not None
    apply_static_version(pyproject, transform)

    assert uv.current_version() == "0.1.1"
    assert uv.preview_bump("patch") == "0.1.2"


def test_rewriting_a_version_file(tmp_path):
    endow(tmp_path)

    result = rewrite_version_files([ENDOW_VERSION_FILE], tmp_path)

    assert result.changed == [tmp_path / ENDOW_VERSION_FILE.path]
    assert result.skipped == []
    assert (tmp_path / ENDOW_VERSION_FILE.path).read_text() == (
        '"""Package metadata for endow."""\n'
        "\n"
        f"{METADATA_IMPORT}\n"
        "\n"
        "__version__ = version(__package__)\n"
    )


def test_a_rewritten_file_is_still_python(tmp_path):
    endow(tmp_path)
    rewrite_version_files([ENDOW_VERSION_FILE], tmp_path)

    compile((tmp_path / ENDOW_VERSION_FILE.path).read_text(), "__about__.py", "exec")


def test_the_import_is_not_added_twice(tmp_path):
    endow(tmp_path)
    (tmp_path / ENDOW_VERSION_FILE.path).write_text(
        f"{METADATA_IMPORT}\n\n__version__ = '0.1.1'\n"
    )

    rewrite_version_files([ENDOW_VERSION_FILE], tmp_path)
    content = (tmp_path / ENDOW_VERSION_FILE.path).read_text()

    assert content.count(METADATA_IMPORT) == 1
    assert "__version__ = version(__package__)" in content


def test_rewriting_keeps_the_indentation(tmp_path):
    (tmp_path / "mod.py").write_text('class Meta:\n    __version__ = "1.0.0"\n')

    rewrite_version_files([VersionFile("mod.py", "__version__")], tmp_path)

    assert (tmp_path / "mod.py").read_text() == (
        f"class Meta:\n    {METADATA_IMPORT}\n\n    __version__ = version(__package__)\n"
    )


def test_files_that_cannot_be_rewritten_are_reported(tmp_path):
    (tmp_path / "version.cfg").write_text("version = '1.0.0'\n")
    (tmp_path / "empty.py").write_text("# no assignment here\n")

    result = rewrite_version_files(
        [
            VersionFile("gone.py", "__version__"),
            VersionFile("version.cfg", "version"),
            VersionFile("empty.py", "__version__"),
        ],
        tmp_path,
    )

    assert result.changed == []
    assert notes(result.skipped) == {
        "gone.py": "no such file",
        "version.cfg": "not a Python file; rewrite it yourself",
        "empty.py": "no __version__ assignment found",
    }


def test_the_plan_pairs_the_freeze_with_the_rewrites(tmp_path):
    plan = plan_version_migration(endow(tmp_path), [ENDOW_VERSION_FILE])

    assert not plan.empty
    assert plan.rewrites == (ENDOW_VERSION_FILE,)
    assert plan.warnings == ()
    assert plan.steps == [
        'set [project].version = "0.1.1" (read from src/endow/__about__.py)',
        'remove "version" from [project].dynamic',
        "remove [tool.hatch.version]",
        "rewrite src/endow/__about__.py to `__version__ = version(__package__)`",
    ]
    assert "dynamic version" in plan.question


def test_a_plan_with_nothing_to_do_is_empty(tmp_path):
    plan = plan_version_migration(write(tmp_path, UV_BUILD), [])

    assert plan.empty
    assert plan.steps == []


def test_the_question_names_what_the_plan_actually_does(tmp_path):
    write(tmp_path, "[project]\nname = 'sample'\nversion = '1.0.0'\n")
    (tmp_path / "sample").mkdir()
    (tmp_path / "sample" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    rewrite_only = plan_version_migration(
        tmp_path / "pyproject.toml", [VersionFile("sample/__init__.py", "__version__")]
    )
    assert "installed metadata" in rewrite_only.question

    write(tmp_path, "[project]\nname = 'sample'\n")
    freeze = plan_version_migration(
        tmp_path / "pyproject.toml", [VersionFile("sample/__init__.py", "__version__")]
    )
    assert "no [project].version" in freeze.question


def test_a_rewrite_that_would_erase_the_only_version_is_refused(tmp_path):
    """
    No [project] table to freeze into, so the file about to be rewritten holds
    the last copy of the version. Refusing beats a project that cannot build.
    """
    write(tmp_path, "[tool.other]\nkey = 1\n")
    (tmp_path / "mod.py").write_text('__version__ = "1.0.0"\n')

    with pytest.raises(VommitError, match="no version at all"):
        plan_version_migration(
            tmp_path / "pyproject.toml", [VersionFile("mod.py", "__version__")]
        )


def test_a_mismatched_distribution_name_is_warned_about(tmp_path):
    write(tmp_path, "[project]\nname = 'my-lib'\nversion = '1.0.0'\n")

    plan = plan_version_migration(
        tmp_path / "pyproject.toml", [VersionFile("src/barfoo/__init__.py", "__ver__")]
    )

    detail = notes(list(plan.warnings))["src/barfoo/__init__.py"]
    assert "'barfoo'" in detail
    assert "'my-lib'" in detail
    assert '__ver__ = version("my-lib")' in detail


def test_a_top_level_module_has_no_package_to_look_up(tmp_path):
    write(tmp_path, "[project]\nname = 'sample'\nversion = '1.0.0'\n")

    plan = plan_version_migration(
        tmp_path / "pyproject.toml", [VersionFile("sample.py", "__version__")]
    )

    detail = notes(list(plan.warnings))["sample.py"]
    assert "no `__package__`" in detail
    assert '__version__ = version("sample")' in detail


@pytest.mark.parametrize(
    "path",
    ["src/my_lib/__init__.py", "my-lib/__init__.py", "./src/my.lib/_version.py"],
)
def test_names_that_normalise_to_the_distribution_stay_quiet(tmp_path, path):
    write(tmp_path, "[project]\nname = 'my-lib'\nversion = '1.0.0'\n")

    plan = plan_version_migration(
        tmp_path / "pyproject.toml", [VersionFile(path, "__version__")]
    )

    assert plan.warnings == ()


def test_a_nested_package_cannot_be_looked_up_either(tmp_path):
    write(tmp_path, "[project]\nname = 'pkg'\nversion = '1.0.0'\n")

    plan = plan_version_migration(
        tmp_path / "pyproject.toml", [VersionFile("src/pkg/sub/_v.py", "__version__")]
    )

    assert "'pkg.sub'" in notes(list(plan.warnings))["src/pkg/sub/_v.py"]


def test_a_project_without_a_name_cannot_be_checked(tmp_path):
    assert project_name(tmp_path / "pyproject.toml") is None
    assert project_name(write(tmp_path, "[tool.other]\nkey = 1\n")) is None
    assert project_name(write(tmp_path, "[project]\nversion = '1.0.0'\n")) is None
    assert project_name(write(tmp_path, "[project]\nname = 'x'\n")) == "x"

    # no name to compare against, so no warning to make
    write(tmp_path, "[project]\nversion = '1.0.0'\n")
    plan = plan_version_migration(
        tmp_path / "pyproject.toml", [VersionFile("whatever.py", "__version__")]
    )
    assert plan.warnings == ()


@pytest.mark.parametrize("policy", ["warn", "error", "skip"])
def test_every_unsupported_policy_is_accepted(policy):
    assert parse_unsupported_policy(policy) == policy


@pytest.mark.parametrize("policy", ["bogus", "Error", "", "WARN"])
def test_an_unrecognised_unsupported_policy_is_refused(policy):
    """
    The option only exists to make things stricter, so it has to fail loudly
    rather than fall back to the laxest setting it has.
    """
    with pytest.raises(VommitError, match="--on-unsupported must be one of"):
        parse_unsupported_policy(policy)


@pytest.mark.parametrize(
    "key,fragment",
    [
        ("commit_author", "your own git identity"),
        ("repository_url", "commands.publish"),
        ("upload_to_pypi_glob_patterns", "commands.publish"),
    ],
)
def test_real_v7_keys_are_not_mistaken_for_typos(tmp_path, key, fragment):
    """
    All three are genuine v7 settings; reporting them as misspellings hides a
    real loss behind a did-you-mean.
    """
    detail = notes(translate(psr(tmp_path, f"{key} = 'x'\n")).report.unsupported)[key]

    assert fragment in detail
    assert "Did you mean" not in detail


def test_stripping_endow(tmp_path):
    pyproject = endow(tmp_path)

    removed = strip_psr(pyproject)
    result = tomlkit.parse(pyproject.read_text())

    assert removed == [
        "[tool.semantic_release]",
        "python-semantic-release from project.optional-dependencies.dev",
    ]
    assert "semantic_release" not in result["tool"]
    assert result["project"]["optional-dependencies"]["dev"] == ["pytest"]
    # the config went, the rest of [tool] did not
    assert "hatch" in result["tool"]


def test_stripping_every_place_a_dependency_can_live(tmp_path):
    pyproject = write(
        tmp_path,
        """
        [project]
        name = "x"
        dependencies = ["Python_Semantic.Release >= 7", "rich"]

        [project.optional-dependencies]
        dev = ["python-semantic-release<8"]

        [dependency-groups]
        release = ["python_semantic_release[dev]==7.34.6", "twine"]

        [tool.semantic_release]
        branch = "main"
        """,
    )

    removed = strip_psr(pyproject)
    result = tomlkit.parse(pyproject.read_text())

    assert len(removed) == 4
    assert result["project"]["dependencies"] == ["rich"]
    assert result["project"]["optional-dependencies"]["dev"] == []
    assert result["dependency-groups"]["release"] == ["twine"]
    # [tool] held nothing else, so it goes too
    assert "tool" not in result


def test_stripping_a_project_with_nothing_to_strip(tmp_path):
    # a [tool] table that simply is not v7's, and dependencies without it
    pyproject = write(
        tmp_path,
        """
        [project]
        name = "x"
        dependencies = ["rich"]

        [tool.ruff]
        line-length = 88
        """,
    )
    before = pyproject.read_text()

    assert strip_psr(pyproject) == []
    assert pyproject.read_text() == before

    # and a project with no [tool] table at all
    other = tmp_path / "other"
    other.mkdir()
    assert strip_psr(write(other, UV_BUILD)) == []


def test_stripping_leaves_untouched_groups_alone(tmp_path):
    pyproject = write(
        tmp_path,
        """
        [project]
        name = "x"

        [project.optional-dependencies]
        dev = ["python-semantic-release<8"]
        docs = ["mkdocs"]

        [tool.semantic_release]
        branch = "main"
        """,
    )

    assert strip_psr(pyproject) == [
        "[tool.semantic_release]",
        "python-semantic-release from project.optional-dependencies.dev",
    ]
    result = tomlkit.parse(pyproject.read_text())
    assert result["project"]["optional-dependencies"]["docs"] == ["mkdocs"]


def test_stripping_keeps_the_array_formatting(tmp_path):
    """
    tomlkit carries the layout; replacing an array wholesale would discard it.
    """
    pyproject = write(
        tmp_path,
        """
        [project]
        name = "x"
        dependencies = [
            "pytest",  # test runner
            "python-semantic-release<8",  # going away
            "rich",
        ]

        [project.optional-dependencies]
        inline = ["python-semantic-release", "mkdocs"]
        only = [
            "python-semantic-release",
        ]
        """,
    )

    strip_psr(pyproject)

    assert pyproject.read_text() == textwrap.dedent(
        """
        [project]
        name = "x"
        dependencies = [
            "pytest",  # test runner
            "rich",
        ]

        [project.optional-dependencies]
        inline = ["mkdocs"]
        only = []
        """
    )


def test_stripping_leaves_an_already_empty_array_as_it_was(tmp_path):
    pyproject = write(
        tmp_path,
        """
        [project]
        name = "x"
        dependencies = [
        ]

        [tool.semantic_release]
        branch = "main"
        """,
    )

    assert strip_psr(pyproject) == ["[tool.semantic_release]"]
    assert "dependencies = [\n]" in pyproject.read_text()
