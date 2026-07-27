# Vommit

> Vommit Oversees Making & Managing Installable Things

## Migrating from python-semantic-release v7

```bash
vommit migrate
```

Reads `[tool.semantic_release]`, reports what survives the translation, and asks what to do with the
result:

- **interactive** — walk through the settings, pre-filled from v7; you are only asked about what v7
  never covered.
- **copy** — write the translated config as it stands.
- **stop** — write nothing. Since the report comes first, a plain run doubles as a dry run.

Then, if the project needs them, two follow-up questions: making the version bumpable, and removing
the old config.

### What it translates

Settings map across where vommit has an equivalent: `branch`, `tag_format`, `commit_subject`,
`changelog_file`, `changelog_placeholder`, `changelog_sections`, `build_command`, `dist_path`,
`prerelease_tag`, the upload switches, and the four `parser_angular_*` keys that decide which commit
types release.

Unset keys come across too. v7 resolved them against its own defaults, and your release history was
produced with those — so a project that configured nothing still gets v7's placeholder, v7's
prerelease token and v7's bump map, not vommit's.

Anything without an equivalent is reported rather than dropped quietly: forge integration, token
variables, the emoji parser settings, `major_on_zero`, and so on. Unknown keys are reported with a
suggestion — v7 ignored typos silently, so a config can carry one for years without anyone noticing.

Pass `--on-unsupported=error` to refuse a config vommit cannot fully honour, or `=skip` to keep the
report short.

### What it refuses

Three shapes are refused before anything is written, because a config that looks migrated but can
never bump is worse than no config at all:

- a `commit_parser` other than the angular one — vommit reads conventional commits only, and
  migrating would silently change which commits count as a release
- `version_toml` pointing anywhere other than `[project].version` (Poetry)
- `version_pattern`, which puts the version behind an arbitrary regex

### Dynamic versions

`uv version` cannot bump a project that declares `dynamic = ["version"]`, whatever the build backend
— and that is the usual v7 shape, since `version_variable` exists precisely because the backend reads
the version out of a source file.

For hatchling and setuptools, `migrate` offers to fix it in one step: read the current version out of
the `version_variable` file (falling back to the latest tag), write it as a static
`[project].version`, drop `dynamic` and the backend's version hook, and point the source file at the
installed metadata instead:

```python
from importlib.metadata import version

__version__ = version(__package__)
```

Both halves happen together. Rewriting `__version__` while the backend still reads its version from
that file would leave the package unbuildable.

### Cleaning up

Last question, defaulting to yes: remove `[tool.semantic_release]` and the `python-semantic-release`
dependency. Two live release configs in one file is the state worth not leaving behind — whichever
tool runs next looks authoritative, and neither is.
