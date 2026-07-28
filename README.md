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
`vommit setup --non-interactive` for defaults. `vommit init --project-name NAME`
creates and configures a new uv package.

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
