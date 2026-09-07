# Vommit

> Vommit Oversees Making & Managing Installable Things

Vommit automates version bumps, changelogs, Git releases, builds, and
publishing for uv-based Python packages using Conventional Commits.

Vommit requires Python 3.12 or newer, [uv](https://docs.astral.sh/uv/) 0.7 or
newer, and a static `[project].version` in `pyproject.toml`, or, for a project
built by maturin, a version in `Cargo.toml` (see
[Crate-backed projects](#crate-backed-projects-maturin)). Automatic version
selection and the default release workflow also expect a Git repository, but
this can be disabled via config.

## Getting started

Install Vommit into the active environment:

```bash
uv pip install vommit
```

For a global installation, you can use something like [uvenv](https://pypi.org/project/uvenv/), `pipx`, or `uv tool`:

```bash
uvenv install vommit
# or: pipx install vommit / uv tool install vommit
```

From a Python project directory, create or complete the Vommit configuration interactively:

```bash
vommit setup
```

To see all commands and options:

```bash
vommit --help
```

## Starting a new project

```bash
vommit init --project-name mypkg
```

Runs `uv init --package`, configures Vommit in the result, and leaves a project
that is ready to release, with a named branch, declared license, changelog, and
initial commit.

The version starts at `0.0.0`, so the first `vommit bump` derives the first
release from its commits: `0.1.0` for a feature or `0.0.1` for a fix.

Each question it asks has a flag that pre-fills it, so `--non-interactive`
takes the answers as given:

```bash
vommit init --project-name mypkg --non-interactive \
    --python 3.13 --license MIT --branch main --venv .venv \
    --remote git@example.com:me/mypkg.git --push
```

| Flag                 | Meaning                                                              |
|----------------------|----------------------------------------------------------------------|
| `--python VERSION`   | Minimum Python version; defaults to the interpreter running Vommit   |
| `--description TEXT` | Omitted from `pyproject.toml` when empty                             |
| `--license SPDX`     | Records `[project].license`; add the `LICENSE` file yourself         |
| `--branch NAME`      | Release branch                                                       |
| `--remote URL`       | Add as `origin`                                                      |
| `--message TEXT`     | Initial commit message; empty makes no commit                        |
| `--push`             | Push that commit and record the upstream                             |
| `--venv NAME`        | Directory for the project's environment (`venv`, `.venv`, or `none`) |
| `--pin-python`       | Keep uv's `.python-version` file                                     |
| `--no-workspace`     | Do not join an enclosing uv workspace                                |

The environment is created inside the new project and installed in editable
mode. Creating it (and installing) runs last and is never fatal: if it fails,
the project is already made, so create it later with `uv venv` yourself.

Run inside an existing repository, `uv init` creates no repository of its own.
Vommit then leaves that history, branch and remote alone, makes no commit, and
writes the `.gitignore` uv skipped: without it a release would commit its own
`dist/`.

## Releasing

```bash
vommit release
```

Vommit bumps the version, runs `clean` and `build`, pushes the release commit and
tag when Git integration is enabled, then publishes. Nothing is pushed if the
build fails; a failed build after a bump can still be taken back with
`vommit bump --undo`.

With Git integration enabled, a release refuses uncommitted changes by default:
the build would include them, but the pushed commit would not. Commit or stash
your work first, or pass `--allow-dirty` to publish the working tree as it
stands. Build output such as `dist/` should be ignored by Git.

A release also refuses to run from a branch other than `git.branch`. Pass
`--allow-branch` to release from the branch that is checked out instead,
typically a prerelease cut from a feature branch. It stays where it is rather
than switching, and the freshness check then runs against that branch's
upstream; `git.on_wrong_branch` is left alone.

| Flag                            | Meaning                                                                                                                          |
|---------------------------------|----------------------------------------------------------------------------------------------------------------------------------|
| `--major`, `--minor`, `--patch` | Choose the version increment                                                                                                     |
| `--prerelease`                  | Create or advance a prerelease                                                                                                   |
| `--version VERSION`             | Set an explicit version                                                                                                          |
| `--allow-dirty`                 | Release despite uncommitted changes                                                                                              |
| `--allow-branch`                | Release from the branch that is checked out, whatever `git.branch` says                                                          |
| `--edit`                        | Write the changelog entry yourself before releasing                                                                              |
| `--yes`                         | Skip confirmations                                                                                                               |
| `--noop`                        | Preview the version, changelog, and commands without running the release                                                         |
| `--no-bump`                     | Run the release pipeline for the current version; useful after a failed upload. Cannot be combined with a version-selection flag |

If a previous attempt left the version tag on different code from `HEAD`,
Vommit explains the mismatch and asks before publishing.

When no commit warrants a bump, `release` asks whether to publish the current
version; `bump` simply stops.

For CI, provide `UV_PUBLISH_TOKEN` and pass `--yes`. A `--noop` run does not
bump, build, push, publish, or resolve a PyPI credential. It still performs Git
preparation checks, which may fetch the remote and, with
`git.on_wrong_branch = "switch"`, switch branches.

### Commands

`[tool.vommit.commands]` holds the release commands. `clean` and `build` run
first; Vommit then pushes the release commit and tag before `publish` and
`post_publish` run. Leave a command empty to skip it.

```toml
[tool.vommit.commands]
clean = "rm -rf ./dist"
build = "uv build"
publish = "uv publish"
post_publish = ""          # runs after the publishing step, even if publish is empty
```

Environment variables such as `$VAR` are expanded when the configuration loads.

`publish` and `post_publish` run only when `pypi.enabled = true`.

### PyPI credentials

```bash
vommit authenticate
```

Stores a PyPI token in your keyring under `vommit`/`pypi`. Vommit checks its
format and verifies it against PyPI before storing it. Use `--no-verify` to
skip these checks.

```bash
vommit authenticate                   # store or replace a token
vommit authenticate --clear           # remove the stored token, then ask for a new one
vommit authenticate --no-verify       # store without checking the token
vommit ensure-authenticated           # ensure a token is available
vommit ensure-authenticated --show    # show its source and a masked value
```

When publishing, Vommit looks in three places, in order:

1. `UV_PUBLISH_TOKEN` in the environment
2. the keyring, when `pypi.use_keyring = true`
3. an interactive prompt

With keyring use enabled, a token entered at the prompt is stored. Set
`pypi.use_keyring = false` to be asked on every release without storing the
answer.

On headless or SSH systems, the default keyring backend may be unavailable.
Install `vommit[ssh]` to add an `ssh-agent`-backed keyring, or disable keyring
use and enter the token for each release.

## Bumping versions and changelogs

```bash
vommit bump
```

`bump` derives the version change from Conventional Commit messages since the
last release: breaking changes are major, `feat` is minor, and `fix` and `perf`
are patch by default. Any other type, `docs` included, bumps nothing on its own;
add it to `version_bump_map` to change that. A `!` in a commit header or a
`BREAKING CHANGE` footer counts as a breaking change; set `allow_breaking_bang`
or `allow_breaking_footer` to `false` to ignore either. Use the version-selection,
`--prerelease`, `--allow-dirty`, `--allow-branch`, `--noop`, `--edit`, and
`--yes` flags to
override or preview the result.

It updates the project version, changelog, and (when Git integration is enabled)
creates the configured release commit and tag. `vommit bump --undo` takes back
the latest local bump, but refuses to rewrite a commit or tag that has already
been pushed.

Prereleases do not receive separate changelog entries by default; their changes
are included when the next stable release is made. Set
`changelog.include_prereleases = true` to list them separately.

### Writing the entry yourself

Generated entries say what the commits said, which is not always what the
release means. `vommit bump --edit` and `vommit release --edit` open the
generated entry in your editor first; what you save is what gets written,
committed and tagged, in the same release commit. The confirmation offers the
same thing without planning for it up front: answer `edit the changelog entry`
instead of yes or no, and Vommit asks again once you are done.

The editor is resolved the way git resolves its own: `$GIT_EDITOR`, then
`core.editor`, then `$VISUAL`, then `$EDITOR`, then whatever is installed. An
editor that returns before the file is saved (`code`, `subl`) needs its
`--wait` flag, or the entry comes back untouched. HTML comments are stripped
from what you save. Saving an empty entry cancels the release; so does closing
the editor with an error. Editing needs a terminal, so `--edit` is refused in
CI rather than silently skipped.

## Configuration

Run `vommit setup` to create or complete `[tool.vommit]` interactively; use
`vommit setup --mode=all` to revisit every setting, or
`vommit setup --non-interactive` for defaults. For a project that does not exist
yet, `vommit init` runs `uv init` first.

### Where the version lives

`setup` detects a literal version in common package files alongside
`[project].version` and offers to consolidate it, so releases do not leave a
second version behind. It offers the same fix for `dynamic = ["version"]`, which
must be frozen before Vommit can bump it. Nothing is rewritten without being
asked; `vommit setup --non-interactive` prints the proposed change and stops.

#### Crate-backed projects (maturin)

A project built by [maturin](https://www.maturin.rs/) keeps its version in
`Cargo.toml` and leaves `pyproject.toml` on `dynamic = ["version"]`, which
`uv version` refuses outright. Vommit recognises that combination and bumps
`[package].version` instead, carrying the new version into `Cargo.lock` and
committing both. No configuration is involved: it applies when the build backend
is `maturin`, `[project]` declares the version dynamic, and `Cargo.toml` states
one. A Python package that merely vendors a crate still releases
`[project].version`.

Versions are written as SemVer and read back as PEP 440, which is the same
translation maturin performs, so the tag and the wheel always agree: Vommit
plans `3.11.0rc1`, writes `3.11.0-rc.1`, and maturin builds
`your_pkg-3.11.0rc1.whl`. Epochs, post-releases, dev-releases, and local versions,
which SemVer cannot express, are refused before anything is written rather
than published under a different number.

#### Build checks

`setup` also reports what would go wrong at build time, because `build` runs
*after* the bump: a broken build is discovered once the version has been
written, the changelog rewritten and the commit tagged. `release` repeats the
same warnings before it bumps anything, so a project that never re-runs `setup`
still hears them.

Warnings about the build command, when `commands.build` runs `uv build`:

| Id                        | Reported when                                                                                                                                                                                                                                                                                 |
|---------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `uv-build-module-missing` | The `uv_build` backend would not find a module where it looks, which happens after renaming `[project].name` without renaming the directory, or with a flat layout, or with a single-file module. Point it somewhere else with `module-name` / `module-root` under `[tool.uv.build-backend]`. |
| `maturin-uv-build`        | The project is built by maturin, where `uv build` produces one wheel for the machine it ran on. Projects shipping several targets set `commands.build` to their own `maturin build --target ...` script.                                                                                      |

Warnings about the build backend, whatever the build command is:

| Id                    | Reported when                                                                                                                                 |
|-----------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| `hatchling-backend`   | `[build-system]` builds with Hatchling instead of `uv_build`.                                                                                 |
| `uv-build-pin`        | `build-backend = "uv_build"` with a `requires` entry outside the range Vommit builds against (`uv_build>=0.12.4,<0.13`), or with none at all. |
| `hatch-build-command` | `commands.build` runs `hatch build`.                                                                                                          |

These are warnings, not refusals; `setup` writes the configuration either way.

`setup` offers to fix each one it can change without losing information: the
backend swap, the pin, and `hatch build` / `hatch publish` when those are the
whole command. The Hatchling swap is offered only for a project whose artifact is
described by nothing but `[build-system]`. A `[tool.hatch.build]`,
`[tool.hatch.version]` or `[tool.hatch.metadata]` table, a dynamic version, a
second build requirement, or a layout `uv_build` does not recognise.
None of these has a mechanical translation, so the warning names what is in
the way instead.

Decline the fix and `setup` offers the other way out: silencing that warning
for this project.

```toml
[tool.vommit]
ignore = ["hatchling-backend", "uv-build-pin"]
```

`--fix` applies those changes without asking, which is the only way a
`--non-interactive` run makes them: on its own that mode prints the warnings
and writes nothing.

```bash
vommit setup --non-interactive --fix=hatchling-backend,hatch-build-command
vommit setup --fix=all   # every warning that has a fix
```

Three ids can carry a fix: `hatchling-backend`, `uv-build-pin` and
`hatch-build-command`. Anything else fails the run, so a misspelled id is an
error rather than a flag that quietly fixes nothing. An id whose fix is
blocked by the project's shape (a `[tool.hatch.build]` table, a `hatch build`
that does more than build) is not an error; the warning names the blocker and
the run says nothing was written.

`release` never applies a fix. It reports the same warnings before the bump and
carries on, because a release is the wrong moment to be changing the build
configuration it is about to run.

The most useful settings to revisit are:

| Setting                                                                         | Purpose                                                                                                                               |
|---------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------|
| `confirm`                                                                       | Turn routine release confirmations on or off.                                                                                         |
| `prerelease_token`                                                              | Choose `alpha`, `beta`, or `rc` for `--prerelease`.                                                                                   |
| `version_bump_map`                                                              | Map Conventional Commit types to version increments.                                                                                  |
| `allow_breaking_bang`, `allow_breaking_footer`                                  | Turn either breaking-change marker on or off.                                                                                         |
| `git.enabled`                                                                   | Turn off Git integration entirely: no commit, tag, or push.                                                                           |
| `git.origin`                                                                    | Select the remote to push to.                                                                                                         |
| `git.branch`, `git.on_wrong_branch`                                             | Select the release branch and how to handle a mismatch (`--allow-branch` overrides it for one run).                                   |
| `git.tag_format`, `git.commit_format`                                           | Format the release tag and commit message. An empty tag format disables tagging; a release needs a commit format when Git is enabled. |
| `git.commit_author`                                                             | Author the release commit as `Name <email>` instead of as yourself.                                                                   |
| `changelog.enabled`                                                             | Turn off changelog updates.                                                                                                           |
| `changelog.file`, `changelog.levels`                                            | Choose the changelog location and displayed commit groups.                                                                            |
| `changelog.placeholder`, `changelog.entry_title_format`                         | Match the insertion marker and format generated entries.                                                                              |
| `pypi.enabled`, `pypi.use_keyring`                                              | Control publishing and credential storage.                                                                                            |
| `commands.clean`, `commands.build`, `commands.publish`, `commands.post_publish` | Define the release pipeline commands.                                                                                                 |
| `ignore`                                                                        | Silence build checks by id (see [Build checks](#build-checks)).                                                                       |

## Migrating from python-semantic-release v7

```bash
vommit migrate
```

Reads `[tool.semantic_release]` from `pyproject.toml`, reports mapped, lossy, and
unsupported settings, then asks whether to review the result, write it, or stop.
Nothing is written before that choice. Use `--yes` to accept the translation,
`--on-unsupported=error` to refuse unsupported settings, or
`--on-unsupported=skip` for a shorter report.

Unsupported configurations are reported rather than silently discarded. Only
`pyproject.toml` is read; `setup.cfg` is not supported.

`upload_to_pypi = false` and `upload_to_repository = false` are reported rather
than carried across: in v7 they usually meant CI or twine did the uploading,
not that the project never published. Publishing stays on by default, an
interactive migration asks, and `pypi.enabled = false` turns it off.

If the project has a dynamic version, Hatchling and setuptools projects can be
fixed during migration: the current version is frozen into
`[project].version`, the backend hook is removed, and configured version files
are changed to read installed metadata:

```python
from importlib.metadata import version

__version__ = version("your-distribution-name")
```

The distribution name is written out rather than derived from `__package__`,
which is whatever the importer called the module (`src.your_package` for a
test importing straight from a checkout) and fails the lookup at import time.

An earlier vommit wrote `version(__package__)`, so `setup` offers to upgrade
that shape too; a file already naming its distribution is never offered.

`[tool.vommit]` is written into the place `[tool.semantic_release]` held, so the
file keeps its shape; without a v7 table to stand in for, a new `[tool.vommit]`
goes at the end. The migration can then remove `[tool.semantic_release]` and the
`python-semantic-release` dependency.
