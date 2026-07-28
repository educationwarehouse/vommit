# Vommit

> Vommit Oversees Making & Managing Installable Things

Vommit automates version bumps, changelogs, Git releases, builds, and
publishing for uv-based Python packages using Conventional Commits.

Vommit requires Python 3.13 or newer, [uv](https://docs.astral.sh/uv/), and a
static `[project].version` in `pyproject.toml`. Automatic version selection and
the default release workflow also expect a Git repository, but this can be
disabled via config.

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
that is ready to release: one commit holding the whole scaffold, on a named
branch, with a license and a changelog.

It asks for the minimum Python version, a description, a license, the release
branch, a Git remote and the first commit message. Every flag pre-fills its
question, so `--non-interactive` takes the answers as given:

```bash
vommit init --project-name mypkg --non-interactive \
    --python 3.13 --license MIT --branch main \
    --remote git@example.com:me/mypkg.git --push
```

| Flag | Meaning |
|---|---|
| `--python VERSION` | Minimum Python version; defaults to the interpreter running Vommit |
| `--description TEXT` | Left out of `pyproject.toml` entirely when empty, rather than carrying uv's placeholder to PyPI |
| `--license SPDX` | Writes `LICENSE`, `[project].license` and `license-files`; `none` writes neither |
| `--branch NAME` | Release branch; renames the one Git created when they differ |
| `--remote URL` | Added as `origin` before the config is written, so the release branch is read from it rather than guessed |
| `--message TEXT` | Initial commit message; empty makes no commit |
| `--push` | Push that commit and record the upstream |
| `--no-sync` | Skip `uv sync`, leaving no `.venv` or `uv.lock` |
| `--pin-python` | Keep uv's `.python-version`, which Vommit otherwise leaves out |
| `--no-workspace` | Do not join an enclosing uv workspace |

`.python-version` is skipped by default: uv pins whichever interpreter it finds
on `PATH`, and a file naming the wrong one quietly holds `uv run` back.
`--python` sets `requires-python` instead, which is the constraint that belongs
in the package.

Licenses Vommit carries the text for are MIT, ISC, BSD-2-Clause and
BSD-3-Clause. Any other SPDX identifier is still recorded in
`[project].license`, but writing the text is left to you: `license-files` would
otherwise point at a file that is not there.

Run inside an existing repository, `uv init` creates no repository of its own.
Vommit then leaves that history, branch and remote alone, makes no commit, and
writes the `.gitignore` uv skipped — without it a release would commit its own
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

| Flag | Meaning |
|---|---|
| `--major`, `--minor`, `--patch` | Choose the version increment |
| `--prerelease` | Create or advance a prerelease |
| `--version VERSION` | Set an explicit version |
| `--allow-dirty` | Release despite uncommitted changes |
| `--yes` | Skip confirmations |
| `--noop` | Preview the version, changelog, and commands without running the release |
| `--no-bump` | Run the release pipeline for the current version; useful after a failed upload. Cannot be combined with a version-selection flag |

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
last release: breaking changes are major, `feat` is minor, and `fix`, `perf`,
and `docs` are patch by default. A `!` in a commit header or a `BREAKING CHANGE`
footer counts as a breaking change; set `allow_breaking_bang` or
`allow_breaking_footer` to `false` to ignore either. Use the version-selection,
`--prerelease`, `--allow-dirty`, `--noop`, and `--yes` flags to override or
preview the result.

It updates the project version, changelog, and (when Git integration is enabled)
creates the configured release commit and tag. `vommit bump --undo` takes back
the latest local bump, but refuses to rewrite a commit or tag that has already
been pushed.

Prereleases do not receive separate changelog entries by default; their changes
are included when the next stable release is made. Set
`changelog.include_prereleases = true` to list them separately.

## Configuration

Run `vommit setup` to create or complete `[tool.vommit]` interactively; use
`vommit setup --mode=all` to revisit every setting, or
`vommit setup --non-interactive` for defaults. For a project that does not exist
yet, `vommit init` runs `uv init` first.

When `git.branch` is left at `<head>`, `setup` pins a concrete name: the remote's
default branch when there is a remote, otherwise the branch that is checked out.
Only with neither does it fall back to `init.defaultBranch`.

### Where the version lives

`setup` also looks for a version written down twice. A literal in
`__about__.py`, `_version.py`, `version.py` or `__init__.py` is a version Vommit
does not bump: the next release moves `[project].version` and leaves
`__version__` behind. It offers to read it from the installed metadata instead,
so there is one version again:

```python
from importlib.metadata import version

__version__ = version(__package__)
```

A project declaring `dynamic = ["version"]` gets the same offer plus the freeze
it needs, because `uv version` — which every bump goes through — refuses a
dynamic version outright. When the version cannot be recovered at all, for
instance with `[tool.hatch.version] source = "vcs"`, `setup` says so and leaves
the rest of the configuration in place.

Nothing is rewritten without being asked: `vommit setup --non-interactive` prints
what it would change and stops.

The most useful settings to revisit are:

| Setting | Purpose |
|---|---|
| `confirm` | Turn routine release confirmations on or off. |
| `prerelease_token` | Choose `alpha`, `beta`, or `rc` for `--prerelease`. |
| `version_bump_map` | Map Conventional Commit types to version increments. |
| `allow_breaking_bang`, `allow_breaking_footer` | Turn either breaking-change marker on or off. |
| `git.enabled` | Turn off Git integration entirely: no commit, tag, or push. |
| `git.origin` | Select the remote to push to. |
| `git.branch`, `git.on_wrong_branch` | Select the release branch and how to handle a mismatch. |
| `git.tag_format`, `git.commit_format` | Format the release tag and commit message. An empty tag format disables tagging; a release needs a commit format when Git is enabled. |
| `changelog.enabled` | Turn off changelog updates. |
| `changelog.file`, `changelog.levels` | Choose the changelog location and displayed commit groups. |
| `changelog.placeholder`, `changelog.entry_title_format` | Match the insertion marker and format generated entries. |
| `pypi.enabled`, `pypi.use_keyring` | Control publishing and credential storage. |
| `commands.clean`, `commands.build`, `commands.publish`, `commands.post_publish` | Define the release pipeline commands. |

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

If the project has a dynamic version, Hatchling and setuptools projects can be
fixed during migration: the current version is frozen into
`[project].version`, the backend hook is removed, and configured version files
are changed to read installed metadata:

```python
from importlib.metadata import version

__version__ = version(__package__)
```

The migration can then remove `[tool.semantic_release]` and the
`python-semantic-release` dependency.
